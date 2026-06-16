import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from paths import root_dir
from tools.base import BaseTool


class SkillInput(BaseModel):
    # LLM调用时传递的skill名字
    name: str = Field(description='Name of the skill')

class SkillTool(BaseTool):
    name: str = Field(description='Name of the skill')
    description: str = Field(description='Description of the skill')
    input_model: SkillInput = Field(description='Input to run the skill')

    def run(self, input_object: SkillInput) -> str:
        pass

    # 扫描并加载指定目录下的所有skill
    def _skill_load(self):
        skill_dir = Path(root_dir / 'skills')
        self.skills = {}
        for skill in skill_dir.rglob('SKILL.md'):
            self.skill_text = skill.read_text(encoding='utf-8', errors='ignore')
            meta, body = self._skill_parse()
            self.skills[meta['name']] = {'name': meta['name'], 'body': body, 'path': str(skill)}

    # 解析skill meta数据
    def _skill_parse(self) -> tuple:
        match = re.match(r"^---\n(.*?)\n---\n(.*)", self.skill_text, re.DOTALL)
        meta = yaml.safe_load(match.group(1))
        return meta, match.group(2)

    # 获取skill描述,构建skill提示词
    def _skill_description(self):
        for skill_item in self.skills.items():
            skill_description = skill_item['meta']

    # 获取skill内容
    def _skill_content(self):
        pass

