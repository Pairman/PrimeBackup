import dataclasses
from typing import List, NamedTuple

from prime_backup.action import Action
from prime_backup.compressors import Compressor
from prime_backup.db.access import DbAccess
from prime_backup.types.blob_info import BlobInfo
from prime_backup.utils import blob_utils, hash_utils


class BadBlobItem(NamedTuple):
	blob: BlobInfo
	desc: str


@dataclasses.dataclass
class VerifyBlobsResult:
	total: int = 0
	ok: int = 0
	invalid: List[BadBlobItem] = dataclasses.field(default_factory=list)  # wierd blobs
	missing: List[BadBlobItem] = dataclasses.field(default_factory=list)  # the file of the blob is missing
	corrupted: List[BadBlobItem] = dataclasses.field(default_factory=list)  # decompress failed
	mismatched: List[BadBlobItem] = dataclasses.field(default_factory=list)  # hash mismatch


class VerifyBlobsAction(Action):
	def run(self) -> VerifyBlobsResult:
		with DbAccess.open_session() as session:
			blobs = list(map(BlobInfo.of, session.get_all_blobs()))

		result = VerifyBlobsResult()
		for blob in blobs:
			if self.is_interrupted.is_set():
				break

			result.total += 1
			blob_path = blob_utils.get_blob_path(blob.hash)

			if not blob_path.is_file():
				result.missing.append(BadBlobItem(blob, 'blob file does not exist'))
				continue

			try:
				compressor = Compressor.create(blob.compress)
			except ValueError:
				result.invalid.append(BadBlobItem(blob, f'unknown compress method {blob.compress!r}'))
				continue

			try:
				with compressor.open_decompressed_bypassed(blob_path) as (reader, f_decompressed):
					sah = hash_utils.calc_reader_size_and_hash(f_decompressed)
			except Exception as e:
				result.invalid.append(BadBlobItem(blob, f'cannot read and decompress blob file: ({type(e)} {e}'))
				continue

			file_size = reader.get_read_len()
			if file_size != blob.stored_size:
				result.corrupted.append(BadBlobItem(blob, f'stored size mismatch, expect {blob.stored_size}, found {file_size}'))
				continue
			if sah.hash != blob.hash:
				result.corrupted.append(BadBlobItem(blob, f'hash mismatch, expect {blob.hash}, found {sah.hash}'))
				continue
			if sah.size != blob.raw_size:
				result.corrupted.append(BadBlobItem(blob, f'raw size mismatch, expect {blob.raw_size}, found {sah.size}'))
				continue

			result.ok += 1

		return result
