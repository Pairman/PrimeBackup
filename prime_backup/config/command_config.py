from mcdreforged.api.utils import Serializable

from prime_backup import constants
from prime_backup.types.units import Duration


class CommandPermissions(Serializable):
	abort: int = 1
	back: int = 2
	confirm: int = 1
	database: int = 4
	delete: int = 2
	delete_range: int = 3
	export: int = 4
	list: int = 1
	make: int = 1
	prune: int = 3
	rename: int = 2
	show: int = 1
	tag: int = 3

	def get(self, literal: str) -> int:
		if literal.startswith('_'):
			raise KeyError(literal)
		return getattr(self, literal, constants.DEFAULT_COMMAND_PERMISSION_LEVEL)

	def items(self):
		return self.serialize().items()


class CommandConfig(Serializable):
	prefix: str = '!!pb'
	permission: CommandPermissions = CommandPermissions()
	confirm_time_wait: Duration = Duration('60s')
	backup_on_restore: bool = True
	restore_countdown_sec: int = 10
