import json
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel
from tools.base import BaseTool
from pydantic import Field, field_validator

class Task(BaseModel):
    id: int = Field(description='Task ID')
    subject: str = Field(description='Subject of the task')
    description: str = Field(description='Description of the task')
    status: str = Field(description='Status of the task. status the available values include: [pending, in_progress, completed]')
    blockedBy: list[int] = Field(description='List of blocked tasks')
    owner: str = Field(description='Owner of the task')

class TaskUpdateModel(BaseModel):
    id: int = Field(description='Task ID')
    subject: Optional[str] = Field(default=None, description='Subject of the task')
    description: Optional[str] = Field(default=None, description='Description of the task')
    status: Optional[Literal['pending', 'in_progress', 'completed']] = Field(default=None, description='Status of the task')
    owner: Optional[str] = Field(default=None, description='Owner of the task')
    blockedBy: Optional[list[int]] = Field(
        default=None,
        description='type: array of integers. Task IDs that block this task. Example: [1, 2, 3]. If empty, omit this field.'
    )
    add_blocked_by: Optional[list[int]] = Field(
        default=None,
        description='type: array of integers. Task IDs to add as blockers. Example: [1, 2]. If none, omit this field.'
    )
    remove_blocked_by: Optional[list[int]] = Field(
        default=None,
        description='type: array of integers. Task IDs to remove as blockers. Example: [1, 2]. If none, omit this field.'
    )

class TaskManager:

    def __init__(self, tasks_dir: Path):
        self.dir = tasks_dir
        self.dir.mkdir(exist_ok=True)
        self._next_id = self._max_id() + 1

    def _max_id(self) -> int:
        task_ids = []
        for file in self.dir.glob('task_*.json'):
            task_ids.append(int(file.stem.split('_')[1]))
        return max(task_ids) if task_ids else 0

    # 加载单条任务
    def _load_task(self, task_id: int) -> Task:
        task_path = self.dir / 'task_{}.json'.format(task_id)

        # 文件不存在
        if not task_path.exists():
            raise FileNotFoundError(task_path)

        task_json = task_path.read_text(encoding='utf-8')

        return Task(**json.loads(task_json))

    # 保存一条任务
    def _save_task(self, task: Task):
        task_path = self.dir / 'task_{}.json'.format(task.id)
        task_path.write_text(json.dumps(task.model_dump(), indent=2, ensure_ascii=False))

    # 创建一条任务
    def create_task(self, task: Task) -> Task:
        task.id = task.id if task.id else self._next_id
        self._save_task(task)
        return task

    # 读取一条任务
    def get_task(self, task_id: int) -> str:
        return json.dumps(self._load_task(task_id), indent=2, ensure_ascii=False)

    # 更新任务
    def update_task(
            self,
            task_id: int,
            status: str = None,
            blocked_by: list[int] = None,
            add_blocked_by: list[int] = None,
            remove_blocked_by: list[int] = None
    ) -> str:
        task: Task = self._load_task(task_id)
        # 设置任务状态
        if status:
            if status not in ['pending', 'in_progress', 'completed']:
                raise ValueError('Invalid status: {}'.format(status))
            task.status = status
        # 如果任务已经是完成状态,则从所有其他任务的 blockedBy 列表中移除已完成的任务.
        if status == 'completed':
            self._clear_dependency(task.id)
        # 设置任务阻塞项
        if blocked_by is not None:
            task.blockedBy = blocked_by
        if add_blocked_by:
            task.blockedBy = sorted(set(task.blockedBy + add_blocked_by))
        # 移除任务阻塞项
        if remove_blocked_by:
            task.blockedBy = [blocked for blocked in task.blockedBy if blocked not in remove_blocked_by]
        # 保存任务
        self._save_task(task)
        return json.dumps(task.model_dump(), indent=2, ensure_ascii=False)

    # 从别的任务中移除已完成任务的依赖
    def _clear_dependency(self, task_id: int):
        for file in self.dir.glob('task_*.json'):
            task = Task(**json.loads(file.read_text(encoding='utf-8')))
            if task_id in task.blockedBy:
                task.blockedBy.remove(task_id)
                self._save_task(task)

    # 任务列表
    def task_list(self) -> str:
        task_files = sorted(self.dir.glob('task_*.json'), key=lambda path: int(path.stem.split('_')[1]))

        # 解析文件JSON到对象
        tasks: list[Task] = []
        for file in task_files:
            task = Task(**json.loads(file.read_text(encoding='utf-8'))).model_dump()
            tasks.append(task)

        # 构造任务列表状态
        lines: list[str] = []
        for task in tasks:
            marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}.get(task.status, "[?]")
            blocked = f" (blocked by: {task.blockedBy})" if task.blockedBy else ""
            lines.append(f"{marker} #{task.id}: {task.subject}{blocked}")
        return "\n".join(lines)


class TaskCreateTool(BaseTool):
    name = "TaskCreate"
    description = "Create a new task."
    input_model = Task

    def __init__(self, task_manager: TaskManager):
        self.task_manager = task_manager

    def run(self, input_object: Task) -> str:
        task = self.task_manager.create_task(task=input_object)
        return json.dumps(task.model_dump(), indent=2, ensure_ascii=False)

class TaskUpdateTool(BaseTool):
    name = "TaskUpdate"
    description = "Update a task's status or dependencies."
    input_model = TaskUpdateModel

    def __init__(self, task_manager: TaskManager):
        self.task_manager = task_manager

    def run(self, input_object: TaskUpdateModel) -> str:
        task = self.task_manager.update_task(
            task_id=input_object.id,
            status=input_object.status,
            blocked_by=input_object.blockedBy,
            add_blocked_by=input_object.add_blocked_by,
            remove_blocked_by=input_object.remove_blocked_by
        )
        return task

class TaskListTool(BaseTool):
    name = "TaskList"
    description = "List all tasks with status summary."
    input_model = {}

    def __init__(self, task_manager: TaskManager):
        self.task_manager = task_manager

    def run(self, input_object: BaseModel) -> str:
        task = self.task_manager.task_list()
        return task

class TaskTool(BaseTool):
    name = "Task"
    description = "Get full details of a task by ID."
    input_model = Task

    def __init__(self, task_manager: TaskManager):
        self.task_manager = task_manager

    def run(self, input_object: Task) -> str:
        task = self.task_manager.get_task(task_id=input_object.id)
        return task
