import json
import logging
import re

logger = logging.getLogger(__name__)


class MultiAgentRouter:
    def resolve_agent(self, message: str, agent_configs: list, default_config=None):
        """Resolve which agent config to use for a message.

        Resolution order:
        1. @agent-name prefix in message
        2. Routing rule keyword match
        3. Default agent
        """
        # Check @mention
        mention_match = re.match(r"^@(\S+)\s*(.*)", message, re.DOTALL)
        if mention_match:
            agent_name = mention_match.group(1)
            remaining_message = mention_match.group(2)
            for config in agent_configs:
                if config.get("name", "").lower() == agent_name.lower():
                    return config, remaining_message

        # Check routing rules
        for config in agent_configs:
            rules = config.get("routing_rules")
            if rules:
                if isinstance(rules, str):
                    rules = json.loads(rules)
                keywords = rules.get("keywords", [])
                for keyword in keywords:
                    if keyword.lower() in message.lower():
                        return config, message

        # Default
        if default_config:
            return default_config, message
        if agent_configs:
            for config in agent_configs:
                if config.get("is_default"):
                    return config, message
            return agent_configs[0], message

        return None, message


multi_agent_router = MultiAgentRouter()
