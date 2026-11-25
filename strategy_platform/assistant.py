"""Bounded strategy-assistant response parsing; provider failures are not replies."""

import json
import re


class AssistantResponseError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def client_restriction(text):
    value = str(text or '').lower()
    return (
        ('当前客户端不支持' in value and ('coding' in value or 'cli' in value))
        or ('仅限' in value and '客户端' in value and ('coding 客户端' in value or '客户端/cli' in value))
        or ('unsupported client' in value and ('coding client' in value or 'cli only' in value))
    )


def parse_assistant_content(content):
    if not isinstance(content, str) or not content.strip() or len(content) > 20000:
        raise AssistantResponseError('ai_response_invalid', 'AI 服务没有返回有效回复，请重试或检查模型配置。')
    content = content.strip()
    if client_restriction(content):
        raise AssistantResponseError('ai_client_restricted', '当前 AI 服务限制客户端类型，不支持此网页后端调用。请联系管理员配置支持服务端 API 调用的接口后重试。')
    if content.startswith('```'):
        match = re.fullmatch(r'```(?:json)?\s*\n?(.*?)\n?```', content, flags=re.S | re.I)
        if not match:
            raise AssistantResponseError('ai_response_invalid', 'AI 回复格式不完整，请重试。')
        content = match.group(1).strip()
    try:
        payload = json.loads(content)
    except ValueError:
        # Plain discussion is useful even if the provider ignores structured
        # output. Broken JSON/HTML gateway output must not masquerade as chat.
        if content.startswith(('{', '[', '<')):
            raise AssistantResponseError('ai_response_invalid', 'AI 服务返回了无法读取的回复，请重试或检查接口配置。')
        return {'reply': content, 'proposal': None}
    if not isinstance(payload, dict) or not isinstance(payload.get('reply', ''), str):
        raise AssistantResponseError('ai_response_invalid', 'AI 服务没有返回有效的策略讨论内容，请重试。')
    reply = payload.get('reply', '').strip()
    proposal = payload.get('proposal')
    if not reply and not (isinstance(proposal, dict) and isinstance(proposal.get('spec'), dict)):
        raise AssistantResponseError('ai_response_invalid', 'AI 服务没有返回有效的策略讨论内容，请重试。')
    if client_restriction(reply):
        raise AssistantResponseError('ai_client_restricted', '当前 AI 服务限制客户端类型，不支持此网页后端调用。请联系管理员配置支持服务端 API 调用的接口后重试。')
    return {'reply': reply, 'proposal': proposal}
