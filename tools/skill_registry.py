from pathlib import Path
from paths import root_dir
import re, yaml

class SkillRegistry:

    def __init__(self, skill_dir: Path):
        self.skill_dir = skill_dir
        self.skills = {}
        self._skill_load()

    # 扫描并加载指定目录下的所有skill
    def _skill_load(self):
        for skill in self.skill_dir.rglob('SKILL.md'):
            self.skill_text = skill.read_text(encoding='utf-8', errors='ignore')
            meta, body = self._skill_parse()
            self.skills[meta['name']] = {'meta': meta, 'body': body, 'path': str(skill)}

    # 解析skill meta数据
    def _skill_parse(self) -> tuple:
        match = re.match(r"^---\n(.*?)\n---\n(.*)", self.skill_text, re.DOTALL)
        meta = yaml.safe_load(match.group(1))
        return meta, match.group(2)

    # 获取skill描述,构建skill提示词
    def skill_description(self) -> str:
        lines = []
        for _name, _value in self.skills.items():
            _description = _value['meta'].get('description', '')
            line = f'   - {_name} : {_description}'
            lines.append(line)
        return '\n'.join(lines)

    # 获取skill内容
    def skill_content(self, name: str) -> str:
        skill = self.skills[name]
        if not skill:
            return f"Error: Unknown skill '{name}'. Available: {', '.join(self.skills.keys())}"
        return f'''
               <skill name="{name}">
                   {skill.get('body')}
               </skill>
               '''