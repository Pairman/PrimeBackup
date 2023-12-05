from mcdreforged.api.all import CommandSource

from prime_backup.action.rename_backup_action import RenameBackupAction
from prime_backup.mcdr.task.basic_tasks import OperationTask
from prime_backup.mcdr.text_components import TextComponents


class RenameBackupTask(OperationTask):
	def __init__(self, source: CommandSource, backup_id: int, comment: str):
		super().__init__(source)
		self.backup_id = backup_id
		self.comment = comment

	@property
	def name(self) -> str:
		return 'rename'

	def run(self) -> None:
		RenameBackupAction(self.backup_id, self.comment).run()
		self.reply(self.tr('modified', TextComponents.backup_id(self.backup_id), repr(self.comment)))
