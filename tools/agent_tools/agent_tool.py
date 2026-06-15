from tools.agent_tools.definition import AgentDefinition, AgentType
from tools.base import BaseTool


tool_name = 'Agent'

tool_description = f'''
Launch a new agent to handle focused, multi-step tasks.\n\n
Available agent types:\n{','.join([item.value for item in AgentType])}\n\n
Usage notes:\n- Include a short 3-5 word description.\n
    - Use this when fresh context prevents polluting the main conversation.\n
    - The subagent returns one final report; intermediate context is discarded.
'''

class AgentDefinitionTool(BaseTool):
    name: str = tool_name
    description: str = tool_description
    input_model = AgentDefinition

    def run(self, input_object: AgentDefinition) -> str:
        # 通用SubAgent
        if input_object.agent_type == AgentType.GENERAL_PURPOSE:
            from tools.agent_tools.general_purpose_agent import GeneralPurposeAgent
            sub_agent = GeneralPurposeAgent(**input_object.model_dump())
            return sub_agent.run(input_object.prompt)
        return 'unknown agent type.'