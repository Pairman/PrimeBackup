import contextlib
import json
import os
import shutil
import stat
import tarfile
import threading
import time
import zipfile
from abc import abstractmethod, ABC
from io import BytesIO
from pathlib import Path
from typing import ContextManager, Optional, List, Tuple, IO, Any, NamedTuple

from prime_backup.action import Action
from prime_backup.compressors import Compressor, CompressMethod
from prime_backup.constants import BACKUP_META_FILE_NAME
from prime_backup.db import schema
from prime_backup.db.access import DbAccess
from prime_backup.db.session import DbSession
from prime_backup.exceptions import PrimeBackupError, VerificationError
from prime_backup.types.backup_meta import BackupMeta
from prime_backup.types.export_failure import ExportFailures
from prime_backup.types.tar_format import TarFormat
from prime_backup.utils import file_utils, blob_utils, misc_utils, hash_utils, path_utils
from prime_backup.utils.bypass_io import BypassReader


class _ExportInterrupted(PrimeBackupError):
	pass


class _ExportBackupActionBase(Action[ExportFailures], ABC):
	def __init__(
			self, backup_id: int, output_path: Path, *,
			fail_soft: bool = False, verify_blob: bool = True, create_meta: bool = True,
	):
		super().__init__()
		self.backup_id = misc_utils.ensure_type(backup_id, int)
		self.output_path = output_path
		self.fail_soft = fail_soft
		self.verify_blob = verify_blob
		self.create_meta = create_meta

	def run(self) -> ExportFailures:
		with DbAccess.open_session() as session:
			backup = session.get_backup(self.backup_id)
			failures = self._export_backup(session, backup)

		if len(failures) > 0:
			self.logger.info('Export done with {} failures'.format(len(failures)))
		else:
			self.logger.info('Export done')
		return failures

	@abstractmethod
	def _export_backup(self, session: DbSession, backup: schema.Backup) -> ExportFailures:
		...

	def _create_meta_buf(self, backup: schema.Backup) -> bytes:
		if not self.create_meta:
			raise RuntimeError('calling _create_meta_buf() with create_meta set to False')
		meta = BackupMeta.from_backup(backup)
		return json.dumps(meta.to_dict(), indent=2, ensure_ascii=False).encode('utf8')

	@classmethod
	def _on_unsupported_file_mode(cls, file: schema.File):
		raise NotImplementedError('file at {!r} with mode={} ({} or {}) is not supported yet'.format(file.path, file.mode, hex(file.mode), oct(file.mode)))

	@classmethod
	def _verify_exported_blob(cls, file: schema.File, written_size: int, written_hash: str):
		if written_size != file.blob_raw_size:
			raise VerificationError('raw size mismatched for {}, expected {}, actual written {}'.format(file.path, file.blob_raw_size, written_size))
		if written_hash != file.blob_hash:
			raise VerificationError('hash mismatched for {}, expected {}, actual written {}'.format(file.path, file.blob_hash, written_hash))


def _i_am_root():
	# reference: tarfile.TarFile.chown
	return hasattr(os, 'geteuid') and os.geteuid() == 0


class _TrashBin:
	def __init__(self, trash_bin_path: Path):
		trash_bin_path.mkdir(parents=True, exist_ok=True)
		self.trash_bin_path = trash_bin_path
		self.trashes: List[Tuple[Path, Path]] = []  # (trash path, original path)

	def add(self, src_path: Path, relpath_in_bin: Path):
		dst_path = self.trash_bin_path / relpath_in_bin
		dst_path.parent.mkdir(parents=True, exist_ok=True)
		src_path.rename(dst_path)
		self.trashes.append((dst_path, src_path))

	def erase(self):
		shutil.rmtree(self.trash_bin_path)

	def restore(self):
		for trash_path, original_path in self.trashes:
			if os.path.lexists(original_path):
				if original_path.is_dir() and not original_path.is_symlink():
					shutil.rmtree(original_path)
				else:
					original_path.unlink()
			trash_path.rename(original_path)

		self.trashes.clear()


class ExportBackupToDirectoryAction(_ExportBackupActionBase):
	class _ExportItem(NamedTuple):
		file: schema.File
		path: Path  # path to export, related to self.output_path
		path_posix: str

	def __init__(
			self, backup_id: int, output_path: Path, *,
			restore_mode: bool = False,
			child_to_export: Optional[Path] = None,
			recursively_export_child: bool = False,
			**kwargs,
	):
		"""
		:param restore_mode: recover what it was like -- delete all backup targets before export
		"""
		super().__init__(backup_id, output_path, **kwargs)
		self.restore_mode = restore_mode
		self.child_to_export = child_to_export
		self.recursively_export_child = recursively_export_child

		if self.restore_mode and self.child_to_export is not None:
			raise ValueError('restore mode does not support exporting child')

	@classmethod
	def __set_attrs(cls, file: schema.File, file_path: Path):
		# reference: tarfile.TarFile.extractall, tarfile.TarFile._extract_member

		is_link = stat.S_ISLNK(file.mode)

		if _i_am_root() and file.uid is not None and file.gid is not None:
			u, g = int(file.uid), int(file.gid)
			if is_link and hasattr(os, 'lchown'):
				os.lchown(file_path, u, g)
			else:
				os.chown(file_path, u, g)

		if not is_link:
			os.chmod(file_path, file.mode)

		if file.atime_ns is not None and file.mtime_ns is not None:
			times = (file.atime_ns / 1e9, file.mtime_ns / 1e9)
			if is_link:
				if os.utime in os.supports_follow_symlinks:
					os.utime(file_path, times, follow_symlinks=False)
			else:
				os.utime(file_path, times)

	def __export_file(self, item: _ExportItem, trash_bin: _TrashBin, exported_directories: List[Tuple[schema.File, Path]]):
		file = item.file
		file_path = self.output_path / item.path

		if os.path.lexists(file_path):
			trash_bin.add(file_path, item.path)

		file_path.parent.mkdir(parents=True, exist_ok=True)
		if stat.S_ISREG(file.mode):
			self.logger.debug('write file {}'.format(file.path))
			blob_path = blob_utils.get_blob_path(file.blob_hash)
			compressor = Compressor.create(file.blob_compress)
			if compressor.get_method() == CompressMethod.plain:
				file_utils.copy_file_fast(blob_path, file_path)
				if self.verify_blob:
					sah = hash_utils.calc_file_size_and_hash(file_path)
					self._verify_exported_blob(file, sah.size, sah.hash)
			else:
				with compressor.open_decompressed(blob_path) as f_in:
					with open(file_path, 'wb') as f_out:
						if self.verify_blob:
							reader = BypassReader(f_in, calc_hash=True)
							shutil.copyfileobj(reader, f_out)
						else:
							reader = None
							shutil.copyfileobj(f_in, f_out)
				if reader is not None:
					self._verify_exported_blob(file, reader.get_read_len(), reader.get_hash())

		elif stat.S_ISDIR(file.mode):
			self.logger.debug('write dir {}'.format(file.path))
			file_path.mkdir(parents=True, exist_ok=True)
			exported_directories.append((file, file_path))

		elif stat.S_ISLNK(file.mode):
			link_target = file.content.decode('utf8')
			os.symlink(link_target, file_path)
			self.logger.debug('write symbolic link {} -> {}'.format(file_path, link_target))
		else:
			self._on_unsupported_file_mode(file)

		if not stat.S_ISDIR(file.mode):
			self.__set_attrs(file, file_path)

	def _export_backup(self, session: DbSession, backup: schema.Backup) -> ExportFailures:
		failures = ExportFailures(self.fail_soft)

		# 1. collect export item

		def add_export_item(file_: schema.File, export_path: Path):
			for t in backup.targets:
				if path_utils.is_relative_to(Path(file_.path), t):
					export_items.append(self._ExportItem(file_, export_path, export_path.as_posix()))
					return
			self.logger.warning('Found out-of-backup-target file, ignored. file.path: {!r}, backup.targets: {}'.format(file, backup.targets))

		export_items: List[ExportBackupToDirectoryAction._ExportItem] = []
		if self.child_to_export is None:
			self.logger.info('Exporting {} to directory {}'.format(backup, self.output_path))
			for file in backup.files:
				add_export_item(file, Path(file.path))
		else:
			self.logger.info('Exporting child {!r} in {} to directory {}, recursively = {}'.format(self.child_to_export.as_posix(), backup, self.output_path, self.recursively_export_child))
			for file in backup.files:
				try:
					rel_path = Path(file.path).relative_to(self.child_to_export)
				except ValueError:
					continue
				if rel_path != Path('.') and not self.recursively_export_child:
					continue
				add_export_item(file, Path(self.child_to_export.name) / rel_path)

		# parent dir first, so self.__export_file will add parent to trash-bin first
		export_items.sort(key=lambda ei: ei.path_posix)

		# 2. do the export

		trash_bin = _TrashBin(self.config.storage_path / 'temp' / 'export_dir_{}_{}'.format(os.getpid(), threading.current_thread().ident))
		try:
			if self.restore_mode:
				# in restore mode, recover what it was like
				# if the backup does not have the target, don't keep the target
				for target in backup.targets:
					target_path = self.output_path / target
					if os.path.lexists(target_path):
						trash_bin.add(target_path, Path(target))

			exported_directories: List[Tuple[schema.File, Path]] = []
			for item in export_items:
				try:
					self.__export_file(item, trash_bin, exported_directories)
				except Exception as e:
					failures.add_or_raise(item.file, e)

			# child dir first
			# reference: tarfile.TarFile.extractall
			for dir_file, dir_file_path in sorted(exported_directories, key=lambda d: d[0].path, reverse=True):
				try:
					self.__set_attrs(dir_file, dir_file_path)
				except Exception as e:
					failures.add_or_raise(dir_file, e)

		except Exception:
			self.logger.warning('Error occurs during export to directory, applying rollback')
			trash_bin.restore()
			raise
		finally:
			trash_bin.erase()

		return failures


class PeekReader:
	def __init__(self, file_obj: IO[bytes], peek_size: int):
		self.file_obj = file_obj
		self.peek_size = peek_size
		self.peek_buf: Optional[bytes] = None
		self.peek_buf_idx = 0

	def peek(self):
		if self.peek_buf is not None:
			raise RuntimeError('double peek')
		self.peek_buf = self.file_obj.read(self.peek_size)

	def read(self, n: int = -1) -> bytes:
		if self.peek_buf is None:
			raise RuntimeError('read before peek')

		if self.peek_buf_idx == len(self.peek_buf):
			return self.file_obj.read(n)

		if n == -1:
			data = self.peek_buf[self.peek_buf_idx:] + self.file_obj.read(n)
			self.peek_buf_idx = len(self.peek_buf)
			return data
		else:
			remaining = len(self.peek_buf) - self.peek_buf_idx
			if n <= remaining:
				data = self.peek_buf[self.peek_buf_idx:self.peek_buf_idx + n]
				self.peek_buf_idx += n
				return data
			else:
				data = self.peek_buf[self.peek_buf_idx:] + self.file_obj.read(n - remaining)
				self.peek_buf_idx = len(self.peek_buf)
				return data


class ExportBackupToTarAction(_ExportBackupActionBase):
	def __init__(self, backup_id: int, output_path: Path, tar_format: TarFormat, **kwargs):
		super().__init__(backup_id, output_path, **kwargs)
		self.tar_format = tar_format

	def is_interruptable(self) -> bool:
		return True

	@contextlib.contextmanager
	def __open_tar(self) -> ContextManager[tarfile.TarFile]:
		with open(self.output_path, 'wb') as f:
			compressor = Compressor.create(self.tar_format.value.compress_method)
			with compressor.compress_stream(f) as f_compressed:
				with tarfile.open(fileobj=f_compressed, mode=self.tar_format.value.mode_w) as tar:
					yield tar

	def __export_file(self, tar: tarfile.TarFile, file: schema.File):
		info = tarfile.TarInfo(name=file.path)
		info.mode = file.mode

		if file.uid is not None:
			info.uid = file.uid
		if file.gid is not None:
			info.gid = file.gid
		if file.mtime_ns is not None:
			info.mtime = int(file.mtime_ns / 1e9)
		if stat.S_ISREG(file.mode):
			self.logger.debug('add file {} to tarfile'.format(file.path))
			info.type = tarfile.REGTYPE
			info.size = file.blob_raw_size
			blob_path = blob_utils.get_blob_path(file.blob_hash)

			with Compressor.create(file.blob_compress).open_decompressed(blob_path) as stream:
				# Exception raised in TarFile.addfile might nuke the whole remaining tar file, which is bad
				# We read a few bytes from the stream, to *hopefully* trigger potential decompress exception in advanced,
				# make it fail before affecting the actual tar file
				peek_reader = PeekReader(stream, 32 * 1024)
				peek_reader.peek()

				if self.verify_blob:
					reader = BypassReader(peek_reader, calc_hash=True)
					tar.addfile(tarinfo=info, fileobj=reader)
				else:
					reader = None
					peek_reader: Any
					tar.addfile(tarinfo=info, fileobj=peek_reader)
			if reader is not None:
				# notes: the read len is always <= info.size
				self._verify_exported_blob(file, reader.get_read_len(), reader.get_hash())

		elif stat.S_ISDIR(file.mode):
			self.logger.debug('add dir {} to tarfile'.format(file.path))
			info.type = tarfile.DIRTYPE
			tar.addfile(tarinfo=info)
		elif stat.S_ISLNK(file.mode):
			self.logger.debug('add symlink {} to tarfile'.format(file.path))
			link_target = file.content.decode('utf8')
			info.type = tarfile.SYMTYPE
			info.linkname = link_target
			tar.addfile(tarinfo=info)
		else:
			self._on_unsupported_file_mode(file)

	def _export_backup(self, session, backup: schema.Backup) -> ExportFailures:
		failures = ExportFailures(self.fail_soft)
		if not self.output_path.name.endswith(self.tar_format.value.extension):
			raise ValueError('bad output file extension for file name {!r}, should be {!r} for tar format {}'.format(
				self.output_path.name, self.tar_format.value.extension, self.tar_format.name,
			))

		self.logger.info('Exporting backup {} to tarfile {}'.format(backup, self.output_path))
		self.output_path.parent.mkdir(parents=True, exist_ok=True)

		try:
			with self.__open_tar() as tar:
				for file in backup.files:
					if self.is_interrupted.is_set():
						self.logger.info('Export to tarfile interrupted')
						raise _ExportInterrupted()
					try:
						self.__export_file(tar, file)
					except Exception as e:
						failures.add_or_raise(file, e)

				if self.create_meta:
					meta_buf = self._create_meta_buf(backup)
					info = tarfile.TarInfo(name=BACKUP_META_FILE_NAME)
					info.mtime = int(time.time())
					info.size = len(meta_buf)
					tar.addfile(tarinfo=info, fileobj=BytesIO(meta_buf))
		except Exception as e:
			with contextlib.suppress(OSError):
				self.output_path.unlink(missing_ok=True)
			if not isinstance(e, _ExportInterrupted):
				raise

		return failures


class ExportBackupToZipAction(_ExportBackupActionBase):
	def is_interruptable(self) -> bool:
		return True

	def __export_file(self, zipf: zipfile.ZipFile, file: schema.File):
		# reference: zipf.writestr -> zipfile.ZipInfo.from_file
		if file.mtime_ns is not None:
			date_time = time.localtime(file.mtime_ns / 1e9)
		else:
			date_time = time.localtime()
		arc_name = file.path
		while len(arc_name) > 0 and arc_name[0] in (os.sep, os.altsep):
			arc_name = arc_name[1:]
		if stat.S_ISDIR(file.mode) and not arc_name.endswith('/'):
			arc_name += '/'

		info = zipfile.ZipInfo(arc_name, date_time[0:6])
		info.external_attr = (file.mode & 0xFFFF) << 16
		info.compress_type = zipf.compression

		if stat.S_ISREG(file.mode):
			self.logger.debug('add file {} to zipfile'.format(file.path))
			info.file_size = file.blob_raw_size
			blob_path = blob_utils.get_blob_path(file.blob_hash)

			with Compressor.create(file.blob_compress).open_decompressed(blob_path) as stream:
				with zipf.open(info, 'w') as zip_item:
					if self.verify_blob:
						reader = BypassReader(stream, calc_hash=True)
						shutil.copyfileobj(reader, zip_item)
					else:
						reader = None
						shutil.copyfileobj(stream, zip_item)
			if reader is not None:
				self._verify_exported_blob(file, reader.get_read_len(), reader.get_hash())

		elif stat.S_ISDIR(file.mode):
			self.logger.debug('add dir {} to zipfile'.format(file.path))
			info.external_attr |= 0x10
			zipf.writestr(info, b'')
		elif stat.S_ISLNK(file.mode):
			self.logger.debug('add symlink {} to zipfile'.format(file.path))
			with zipf.open(info, 'w') as zip_item:
				zip_item.write(file.content)
		else:
			self._on_unsupported_file_mode(file)

	def _export_backup(self, session, backup: schema.Backup) -> ExportFailures:
		failures = ExportFailures(self.fail_soft)
		self.logger.info('Exporting backup {} to zipfile {}'.format(backup, self.output_path))
		self.output_path.parent.mkdir(parents=True, exist_ok=True)

		try:
			with zipfile.ZipFile(self.output_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
				for file in backup.files:
					if self.is_interrupted.is_set():
						self.logger.info('Export to zipfile interrupted')
						raise _ExportInterrupted()
					try:
						self.__export_file(zipf, file)
					except Exception as e:
						failures.add_or_raise(file, e)

				if self.create_meta:
					meta_buf = self._create_meta_buf(backup)
					info = zipfile.ZipInfo(BACKUP_META_FILE_NAME, time.localtime()[0:6])
					info.compress_type = zipf.compression
					info.file_size = len(meta_buf)
					with zipf.open(info, 'w') as f:
						f.write(meta_buf)

		except Exception as e:
			with contextlib.suppress(OSError):
				self.output_path.unlink(missing_ok=True)
			if not isinstance(e, _ExportInterrupted):
				raise

		return failures
