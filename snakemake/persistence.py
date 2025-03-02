__author__ = "Johannes Köster"
__copyright__ = "Copyright 2022, Johannes Köster"
__email__ = "johannes.koester@uni-due.de"
__license__ = "MIT"

import os
import shutil
import signal
import marshal
import pickle
import json
import stat
import tempfile
import time
from base64 import urlsafe_b64encode, b64encode
from functools import lru_cache, partial
from itertools import filterfalse, count
from pathlib import Path

import snakemake.exceptions
from snakemake.logging import logger
from snakemake.jobs import jobfiles
from snakemake.utils import listfiles
from snakemake.io import is_flagged, get_flag_value


UNREPRESENTABLE = object()


class Persistence:
    def __init__(
        self,
        nolock=False,
        dag=None,
        conda_prefix=None,
        singularity_prefix=None,
        shadow_prefix=None,
        warn_only=False,
    ):
        try:
            import pandas as pd

            self._serialize_param = self._serialize_param_pandas
        except ImportError:
            self._serialize_param = self._serialize_param_builtin

        self._max_len = None
        self.path = os.path.abspath(".snakemake")
        if not os.path.exists(self.path):
            os.mkdir(self.path)
        self._lockdir = os.path.join(self.path, "locks")
        if not os.path.exists(self._lockdir):
            os.mkdir(self._lockdir)

        self.dag = dag
        self._lockfile = dict()

        self._metadata_path = os.path.join(self.path, "metadata")
        self._incomplete_path = os.path.join(self.path, "incomplete")

        self.conda_env_archive_path = os.path.join(self.path, "conda-archive")
        self.benchmark_path = os.path.join(self.path, "benchmarks")

        self.source_cache = os.path.join(self.path, "source_cache")

        if conda_prefix is None:
            self.conda_env_path = os.path.join(self.path, "conda")
        else:
            self.conda_env_path = os.path.abspath(os.path.expanduser(conda_prefix))
        if singularity_prefix is None:
            self.container_img_path = os.path.join(self.path, "singularity")
        else:
            self.container_img_path = os.path.abspath(
                os.path.expanduser(singularity_prefix)
            )
        if shadow_prefix is None:
            self.shadow_path = os.path.join(self.path, "shadow")
        else:
            self.shadow_path = os.path.join(shadow_prefix, "shadow")

        # place to store any auxiliary information needed during a run (e.g. source tarballs)
        self.aux_path = os.path.join(self.path, "auxiliary")

        # migration of .snakemake folder structure
        migration_indicator = Path(
            os.path.join(self._incomplete_path, "migration_underway")
        )
        if (
            os.path.exists(self._metadata_path)
            and not os.path.exists(self._incomplete_path)
        ) or migration_indicator.exists():
            os.makedirs(self._incomplete_path, exist_ok=True)

            migration_indicator.touch()

            self.migrate_v1_to_v2()

            migration_indicator.unlink()

        self._incomplete_cache = None

        for d in (
            self._metadata_path,
            self._incomplete_path,
            self.shadow_path,
            self.conda_env_archive_path,
            self.conda_env_path,
            self.container_img_path,
            self.aux_path,
        ):
            os.makedirs(d, exist_ok=True)

        if nolock:
            self.lock = self.noop
            self.unlock = self.noop
        if warn_only:
            self.lock = self.lock_warn_only
            self.unlock = self.noop

        self._read_record = self._read_record_cached

    def migrate_v1_to_v2(self):
        logger.info("Migrating .snakemake folder to new format...")
        i = 0
        for path, _, filenames in os.walk(self._metadata_path):
            path = Path(path)
            for filename in filenames:
                with open(path / filename, "r") as f:
                    try:
                        record = json.load(f)
                    except json.JSONDecodeError:
                        continue  # not a properly formatted JSON file

                    if record.get("incomplete", False):
                        target_path = Path(self._incomplete_path) / path.relative_to(
                            self._metadata_path
                        )
                        os.makedirs(target_path, exist_ok=True)
                        shutil.copyfile(
                            path / filename,
                            target_path / filename,
                        )
                i += 1
                # this can take a while for large folders...
                if (i % 10000) == 0 and i > 0:
                    logger.info("{} files migrated".format(i))

        logger.info("Migration complete")

    @property
    def files(self):
        if self._files is None:
            self._files = set(self.dag.output_files)
        return self._files

    def lock_conflicts(self):
        inputfiles = set(self.all_inputfiles())
        outputfiles = set(self.all_outputfiles())
        if os.path.exists(self._lockdir):
            for lockfile in self._locks("input"):
                with open(lockfile) as lock:
                    for f in lock:
                        f = f.strip()
                        if f in outputfiles:
                            return f
            for lockfile in self._locks("output"):
                with open(lockfile) as lock:
                    for f in lock:
                        f = f.strip()
                        if f in outputfiles or f in inputfiles:
                            return f
        return None

    @property
    def locked(self):
        if self.lock_conflicts():
            return True
        else:
            return False

    def lock_warn_only(self):
        lock_conflict = self.lock_conflicts()
        if lock_conflict:
            logger.info(
                "Error: Directory cannot be locked. This usually "
                "means that another Snakemake instance is running on this directory. "
                "Another possibility is that a previous run exited unexpectedly."
                f"File(s) of interest include the following:\n {lock_conflict}"
            )

    def lock(self):
        lock_conflict = self.lock_conflicts()
        if lock_conflict:
            raise snakemake.exceptions.LockException(lock_conflict)
        self._lock(self.all_inputfiles(), "input")
        self._lock(self.all_outputfiles(), "output")

    def unlock(self, *args):
        logger.debug("unlocking")
        for lockfile in self._lockfile.values():
            try:
                logger.debug("removing lock")
                os.remove(lockfile)
            except OSError as e:
                if e.errno != 2:  # missing file
                    raise e
        logger.debug("removed all locks")

    def cleanup_locks(self):
        shutil.rmtree(self._lockdir)

    def cleanup_metadata(self, path):
        return self._delete_record(self._metadata_path, path)

    def cleanup_shadow(self):
        if os.path.exists(self.shadow_path):
            shutil.rmtree(self.shadow_path)
            os.mkdir(self.shadow_path)

    def cleanup_containers(self):
        from humanfriendly import format_size

        required_imgs = {Path(img.path) for img in self.dag.container_imgs.values()}
        img_dir = Path(self.container_img_path)
        total_size_cleaned_up = 0
        num_containers_removed = 0
        for pulled_img in img_dir.glob("*.simg"):
            if pulled_img in required_imgs:
                continue
            size_bytes = pulled_img.stat().st_size
            total_size_cleaned_up += size_bytes
            filesize = format_size(size_bytes)
            pulled_img.unlink()
            logger.debug(f"Removed unrequired container {pulled_img} ({filesize})")
            num_containers_removed += 1

        if num_containers_removed == 0:
            logger.info("No containers require cleaning up")
        else:
            logger.info(
                f"Cleaned up {num_containers_removed} containers, saving {format_size(total_size_cleaned_up)}"
            )

    def conda_cleanup_envs(self):
        # cleanup envs
        in_use = set(env.hash[:8] for env in self.dag.conda_envs.values())
        for d in os.listdir(self.conda_env_path):
            if len(d) >= 8 and d[:8] not in in_use:
                if os.path.isdir(os.path.join(self.conda_env_path, d)):
                    shutil.rmtree(os.path.join(self.conda_env_path, d))
                else:
                    os.remove(os.path.join(self.conda_env_path, d))

        # cleanup env archives
        in_use = set(env.content_hash for env in self.dag.conda_envs.values())
        for d in os.listdir(self.conda_env_archive_path):
            if d not in in_use:
                shutil.rmtree(os.path.join(self.conda_env_archive_path, d))

    def started(self, job, external_jobid=None):
        for f in job.output:
            self._record(
                self._incomplete_path,
                {"external_jobid": external_jobid},
                f,
            )

    def finished(self, job, keep_metadata=True):
        if not keep_metadata:
            for f in job.expanded_output:
                self._delete_record(self._incomplete_path, f)
            return

        version = str(job.rule.version) if job.rule.version is not None else None
        code = self._code(job.rule)
        input = self._input(job)
        log = self._log(job)
        params = self._params(job)
        shellcmd = job.shellcmd
        conda_env = self._conda_env(job)
        fallback_time = time.time()
        for f in job.expanded_output:
            rec_path = self._record_path(self._incomplete_path, f)
            starttime = os.path.getmtime(rec_path) if os.path.exists(rec_path) else None
            # Sometimes finished is called twice, if so, lookup the previous starttime
            if not os.path.exists(rec_path):
                starttime = self._read_record(self._metadata_path, f).get(
                    "starttime", None
                )
            endtime = f.mtime.local_or_remote() if f.exists else fallback_time

            checksums = ((infile, infile.checksum()) for infile in job.input)

            self._record(
                self._metadata_path,
                {
                    "version": version,
                    "code": code,
                    "rule": job.rule.name,
                    "input": input,
                    "log": log,
                    "params": params,
                    "shellcmd": shellcmd,
                    "incomplete": False,
                    "starttime": starttime,
                    "endtime": endtime,
                    "job_hash": hash(job),
                    "conda_env": conda_env,
                    "container_img_url": job.container_img_url,
                    "input_checksums": {
                        infile: checksum
                        for infile, checksum in checksums
                        if checksum is not None
                    },
                },
                f,
            )
            self._delete_record(self._incomplete_path, f)

    def cleanup(self, job):
        for f in job.expanded_output:
            self._delete_record(self._incomplete_path, f)
            self._delete_record(self._metadata_path, f)

    def incomplete(self, job):
        if self._incomplete_cache is None:
            self._cache_incomplete_folder()

        if self._incomplete_cache is False:  # cache deactivated

            def marked_incomplete(f):
                return self._exists_record(self._incomplete_path, f)

        else:

            def marked_incomplete(f):
                rec_path = self._record_path(self._incomplete_path, f)
                return rec_path in self._incomplete_cache

        return any(map(lambda f: f.exists and marked_incomplete(f), job.output))

    def _cache_incomplete_folder(self):
        self._incomplete_cache = {
            os.path.join(path, f)
            for path, dirnames, filenames in os.walk(self._incomplete_path)
            for f in filenames
        }

    def external_jobids(self, job):
        return list(
            set(
                self._read_record(self._incomplete_path, f).get("external_jobid", None)
                for f in job.output
            )
        )

    def metadata(self, path):
        return self._read_record(self._metadata_path, path)

    def version(self, path):
        return self.metadata(path).get("version")

    def rule(self, path):
        return self.metadata(path).get("rule")

    def input(self, path):
        return self.metadata(path).get("input")

    def log(self, path):
        return self.metadata(path).get("log")

    def shellcmd(self, path):
        return self.metadata(path).get("shellcmd")

    def params(self, path):
        return self.metadata(path).get("params")

    def code(self, path):
        return self.metadata(path).get("code")

    def conda_env(self, path):
        return self.metadata(path).get("conda_env")

    def container_img_url(self, path):
        return self.metadata(path).get("container_img_url")

    def input_checksums(self, job, input_path):
        """Return all checksums of the given input file
        recorded for the output of the given job.
        """
        return set(
            self.metadata(output_path).get("input_checksums", {}).get(input_path)
            for output_path in job.output
        )

    def version_changed(self, job, file=None):
        """Yields output files with changed versions or bool if file given."""
        return _bool_or_gen(self._version_changed, job, file=file)

    def code_changed(self, job, file=None):
        """Yields output files with changed code or bool if file given."""
        return _bool_or_gen(self._code_changed, job, file=file)

    def input_changed(self, job, file=None):
        """Yields output files with changed input or bool if file given."""
        return _bool_or_gen(self._input_changed, job, file=file)

    def params_changed(self, job, file=None):
        """Yields output files with changed params or bool if file given."""
        return _bool_or_gen(self._params_changed, job, file=file)

    def conda_env_changed(self, job, file=None):
        """Yields output files with changed conda env or bool if file given."""
        return _bool_or_gen(self._conda_env_changed, job, file=file)

    def container_changed(self, job, file=None):
        """Yields output files with changed container img or bool if file given."""
        return _bool_or_gen(self._container_changed, job, file=file)

    def _version_changed(self, job, file=None):
        assert file is not None
        recorded = self.version(file)
        return recorded is not None and recorded != job.rule.version

    def _code_changed(self, job, file=None):
        assert file is not None
        recorded = self.code(file)
        return recorded is not None and recorded != self._code(job.rule)

    def _input_changed(self, job, file=None):
        assert file is not None
        recorded = self.input(file)
        return recorded is not None and recorded != self._input(job)

    def _params_changed(self, job, file=None):
        assert file is not None
        recorded = self.params(file)
        return recorded is not None and recorded != self._params(job)

    def _conda_env_changed(self, job, file=None):
        assert file is not None
        recorded = self.conda_env(file)
        return recorded is not None and recorded != self._conda_env(job)

    def _container_changed(self, job, file=None):
        assert file is not None
        recorded = self.container_img_url(file)
        return recorded is not None and recorded != job.container_img_url

    def noop(self, *args):
        pass

    def _b64id(self, s):
        return urlsafe_b64encode(str(s).encode()).decode()

    @lru_cache()
    def _code(self, rule):
        code = rule.run_func.__code__
        return b64encode(pickle_code(code)).decode()

    @lru_cache()
    def _conda_env(self, job):
        if job.conda_env:
            return b64encode(job.conda_env.content).decode()

    @lru_cache()
    def _input(self, job):
        get_path = (
            lambda f: get_flag_value(f, "sourcecache_entry").get_path_or_uri()
            if is_flagged(f, "sourcecache_entry")
            else f
        )
        return sorted(get_path(f) for f in job.input)

    @lru_cache()
    def _log(self, job):
        return sorted(job.log)

    def _serialize_param_builtin(self, param):
        if param is None or isinstance(
            param,
            (
                int,
                float,
                bool,
                str,
                complex,
                range,
                list,
                tuple,
                dict,
                set,
                frozenset,
                bytes,
                bytearray,
            ),
        ):
            return repr(param)
        else:
            return UNREPRESENTABLE

    def _serialize_param_pandas(self, param):
        import pandas as pd

        if isinstance(param, (pd.DataFrame, pd.Series, pd.Index)):
            return repr(pd.util.hash_pandas_object(param).tolist())
        return self._serialize_param_builtin(param)

    @lru_cache()
    def _params(self, job):
        return sorted(
            filter(
                lambda p: p is not UNREPRESENTABLE,
                map(self._serialize_param, job.params),
            )
        )

    @lru_cache()
    def _output(self, job):
        return sorted(job.output)

    def _record(self, subject, json_value, id):
        recpath = self._record_path(subject, id)
        recdir = os.path.dirname(recpath)
        os.makedirs(recdir, exist_ok=True)
        # Write content to temporary file and rename it to the final file.
        # This avoids race-conditions while writing (e.g. on NFS when the main job
        # and the cluster node job propagate their content and the system has some
        # latency including non-atomic propagation processes).
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=recdir,
            delete=False,
            # Add short prefix to final filename for better debugging.
            # This may not be the full one, because that may be too long
            # for the filesystem in combination with the prefix from the temp
            # file.
            suffix=f".{os.path.basename(recpath)[:8]}",
        ) as tmpfile:
            json.dump(json_value, tmpfile)
        # ensure read and write permissions for user and group
        os.chmod(
            tmpfile.name, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IWGRP
        )
        os.replace(tmpfile.name, recpath)

    def _delete_record(self, subject, id):
        try:
            recpath = self._record_path(subject, id)
            os.remove(recpath)
            recdirs = os.path.relpath(os.path.dirname(recpath), start=subject)
            if recdirs != ".":
                os.removedirs(recdirs)
            return True
        except OSError as e:
            if e.errno != 2:
                # not missing
                raise e
            else:
                # file is missing, report failure
                return False

    @lru_cache()
    def _read_record_cached(self, subject, id):
        return self._read_record_uncached(subject, id)

    def _read_record_uncached(self, subject, id):
        if not self._exists_record(subject, id):
            return dict()
        with open(self._record_path(subject, id), "r") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError as e:
                pass
        # case: file is corrupted, delete it
        logger.warning(f"Deleting corrupted metadata record.")
        self._delete_record(subject, id)
        return dict()

    def _exists_record(self, subject, id):
        return os.path.exists(self._record_path(subject, id))

    def _locks(self, type):
        return (
            f
            for f, _ in listfiles(
                os.path.join(self._lockdir, "{{n,[0-9]+}}.{}.lock".format(type))
            )
            if not os.path.isdir(f)
        )

    def _lock(self, files, type):
        for i in count(0):
            lockfile = os.path.join(self._lockdir, "{}.{}.lock".format(i, type))
            if not os.path.exists(lockfile):
                self._lockfile[type] = lockfile
                with open(lockfile, "w") as lock:
                    print(*files, sep="\n", file=lock)
                return

    def _fetch_max_len(self, subject):
        if self._max_len is None:
            self._max_len = os.pathconf(subject, "PC_NAME_MAX")
        return self._max_len

    def _record_path(self, subject, id):
        max_len = (
            self._fetch_max_len(subject) if os.name == "posix" else 255
        )  # maximum NTFS and FAT32 filename length
        if max_len == 0:
            max_len = 255

        b64id = self._b64id(id)
        # split into chunks of proper length
        b64id = [b64id[i : i + max_len - 1] for i in range(0, len(b64id), max_len - 1)]
        # prepend dirs with @ (does not occur in b64) to avoid conflict with b64-named files in the same dir
        b64id = ["@" + s for s in b64id[:-1]] + [b64id[-1]]
        path = os.path.join(subject, *b64id)
        return path

    def all_outputfiles(self):
        # we only look at output files that will be updated
        return jobfiles(self.dag.needrun_jobs(), "output")

    def all_inputfiles(self):
        # we consider all input files, also of not running jobs
        return jobfiles(self.dag.jobs, "input")

    def deactivate_cache(self):
        self._read_record_cached.cache_clear()
        self._read_record = self._read_record_uncached
        self._incomplete_cache = False


def _bool_or_gen(func, job, file=None):
    if file is None:
        return (f for f in job.expanded_output if func(job, file=f))
    else:
        return func(job, file=file)


def pickle_code(code):
    consts = [
        (pickle_code(const) if type(const) == type(code) else const)
        for const in code.co_consts
    ]
    return pickle.dumps((code.co_code, code.co_varnames, consts, code.co_names))
