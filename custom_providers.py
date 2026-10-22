'''
第三方模型源（自定义 API 端点）的注册、探测与归一化。

解决的问题
----------
原先「模型配置」里只有 5 个写死的 provider（deepseek / zhipu / openrouter /
ollama / llamacpp）：模型列表硬编码、切换时 base_url 查字典、密钥只能塞进
已有条目。用户想接任意第三方（如 tokenrhythm.studio、硅基流动、各种中转站、
LM Studio、自建 one-api）根本没有入口。

本模块把「第三方源」变成一等公民：
    model_config.json → "custom_providers": [ {...}, {...} ]   ← 无限添加

兼容大部分第三方靠三件事
------------------------
1. **base_url 归一化**：用户会把各种形态粘进来 ——
      https://tokenrhythm.studio
      https://tokenrhythm.studio/v1
      https://tokenrhythm.studio/v1/chat/completions   ← 整段端点
      api.siliconflow.cn
   全部收敛成同一种「API 根」形态；纯域名自动补 /v1，已经是
   /api/paas/v4 这类路径的原样保留（智谱那种风格）。
2. **候选端点回退**：归一化后仍按优先级试多个 URL（base+/models、
   host+/v1/models、host+/models），单个站点不合惯例也能连上。
3. **协议自动识别**：先试 OpenAI 的 GET /v1/models，失败就用最小
   chat 请求探活（很多站点不实现 /models），再试 Anthropic 的
   POST /v1/messages。识别结果直接告诉用户，并指出与所选协议是否不一致。

安全边界：本模块只为「用户自己填的地址」发请求（本地 App，用户主控），
所以**不**做内网 SSRF 拦截 —— 接 127.0.0.1 的 LM Studio / Ollama 正是
常见用法。（web-proxy 那套 SSRF 防护是另一回事，别混用。）
'''

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
BUILTIN_IDS = {'glm', 'local', 'zhipu', 'custom', 'ollama', 'openai', 'deepseek', 'llamacpp', 'lmstudio', 'anthropic', 'openrouter'}
PROTOCOLS = ('openai', 'anthropic')
_ENDPOINT_SUFFIXES = ('/chat/completions', '/completions', '/messages', '/embeddings', '/images/generations', '/responses', '/models')
_SCHEME_RE = re.compile('^[a-z][a-z0-9+.\\-]*://', re.I)
_URL_RE = re.compile('^(?P<scheme>[a-z][a-z0-9+.\\-]*://)(?P<host>[^/]+)(?P<path>/.*)?$', re.I)

def _config_path(root):
    return os.path.join(root, 'model_config.json')

def read_raw(root):
    '''读整个 model_config.json（失败返回 {}）。'''

    try:
        with open(_config_path(root), encoding='utf-8') as f:
            return json.load(f) or {}
    except Exception:
        return {}

def load_all(root):
    '''返回全部自定义源（已做字段归一，保证下游不必判 None）。'''

    raw = read_raw(root)
    items = raw.get('custom_providers')
    if not isinstance(items, list):
        return []
    out = []
    for it in items:
        if isinstance(it, dict):
            if not (it.get('id') and it.get('base_url')):
                continue
            out.append(_normalize_item(it))
    return out

def save_all(root, items):
    '''整表覆盖写回（只动 custom_providers 一个键，其余原样保留）。'''

    path = _config_path(root)
    raw = read_raw(root)
    raw['custom_providers'] = [_for_storage(i) for i in items]
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(raw, f, ensure_ascii=False, indent=4)
    os.replace(tmp, path)

def get(root, provider_id):
    for it in load_all(root):
        if not it['id'] == provider_id:
            continue
        return it

def upsert(root, item):
    '''新增或更新一个源。返回 (保存后的条目, 是否新建, 查重错误)。

查重规则（2026-09-19 按用户要求定稿）：
- 同站点 + **不同密钥** = 独立条目，正常新建（同一个站注册两个号很常见，
  靠备注区分；早先"按地址去重覆盖"会把第一条冲掉，就是用户投诉的 bug）。
- 同站点 + 同密钥 = 真重复，拦下（重复点「保存」不该变成两份一样的源）。
- 备注重名 = 拦下，要求改名（列表里两个同名条目没法分清谁是谁）。
  编辑时排除自身再查，允许"不改名只改别处"。

错误时不落盘，返回 (None, False, 错误文案)。
'''

    items = load_all(root)
    pid = (item.get('id') or '').strip()
    name = str(item.get('name') or '').strip()
    if name:
        for old in items:
            if old['id'] == pid:
                continue
            if str(old.get('name') or '').strip() == name:
                return None, False, f'已有一个叫「{name}」的源，请换个备注区分（比如加上账号别名）'
    if not pid:
        if item.get('base_url'):
            want = normalize_base_url(item['base_url'], item.get('protocol') or 'openai')
            for old in items:
                if old['api_base'] != want:
                    continue
                if (old.get('api_key') or '') != (item.get('api_key') or ''):
                    continue
                old_name = str(old.get('name') or '').strip() or want
                return None, False, f'「{old_name}」已经添加过这个站点 + 这把密钥，直接用它即可；要用不同的密钥请先填上新密钥再保存'
    if pid:
        for i, old in enumerate(items):
            if old['id'] == pid:
                merged = dict(old)
                merged.update({k: v for k, v in item.items() if v is not None})
                merged['id'] = pid
                merged['updated_at'] = int(time.time())
                items[i] = _normalize_item(merged)
                save_all(root, items)
                return items[i], False, ''
    if not pid:
        pid = make_id(root, item.get('base_url', ''), item.get('name', ''))
        created = True
    new = dict(item)
    new['id'] = pid
    new['created_at'] = new.get('created_at') or int(time.time())
    new['updated_at'] = int(time.time())
    new = _normalize_item(new)
    items.append(new)
    save_all(root, items)
    return new, created, ''

def remove(root, provider_id):
    items = load_all(root)
    left = [i for i in items if i['id'] != provider_id]
    if len(left) == len(items):
        return False
    save_all(root, left)
    return True

def _normalize_item(it):
    pid = str(it.get('id') or '').strip()
    base = str(it.get('base_url') or '').strip()
    protocol = str(it.get('protocol') or 'openai').strip().lower()
    if protocol not in PROTOCOLS:
        protocol = 'openai'
    models = []
    for m in it.get('models') or []:
        if isinstance(m, dict):
            mid = str(m.get('id') or '').strip()
            if mid:
                models.append({'id': mid, 'name': str(m.get('name') or '').strip()})
        else:
            mid = str(m or '').strip()
            if mid:
                models.append({'id': mid, 'name': ''})
    seen, uniq = set(), []
    for m in models:
        if m['id'] in seen:
            continue
        seen.add(m['id'])
        uniq.append(m)
    try:
        ctx = int(it.get('ctx_window') or 0)
    except Exception:
        ctx = 0
    return {'id': pid, 'name': str(it.get('name') or pid).strip() or pid, 'protocol': protocol, 'base_url': base, 'api_base': normalize_base_url(base, protocol), 'api_key': str(it.get('api_key') or ''), 'models': uniq, 'headers': dict(it.get('headers') or {}) if isinstance(it.get('headers'), dict) else {}, 'vision': bool(it.get('vision', False)), 'enabled': bool(it.get('enabled', True)), 'ctx_window': ctx, 'note': str(it.get('note') or ''), 'balance_manual': _norm_manual(it.get('balance_manual')), 'balance_auth': _norm_bal_auth(it.get('balance_auth')), 'extra_body': _norm_body(it.get('extra_body')), 'created_at': int(it.get('created_at') or 0), 'updated_at': int(it.get('updated_at') or 0)}

def _norm_manual(m):
    '''手填余额：{"value": float, "currency": str, "note": str, "updated_at": int}。'''

    if not isinstance(m, dict):
        return {}
    v = _qnum(m.get('value'))
    if v is None:
        return {}
    return {'value': v, 'currency': str(m.get('currency') or '').strip().upper(), 'note': str(m.get('note') or '').strip()[:120], 'updated_at': int(m.get('updated_at') or 0)}

def _public_bal_auth(a):
    '''给前端的平台凭据形态：令牌只给脱敏，用户 ID 可回显（它不是秘密）。'''

    a = a if isinstance(a, dict) else {}
    tok = str(a.get('system_token') or '')
    return {'has_token': bool(tok), 'token_masked': mask_key(tok), 'user_id': str(a.get('user_id') or '')}

def _norm_bal_auth(a):
    '''平台管理凭据：{"system_token": str, "user_id": str}。

    为什么单独开一格：DMXAPI 这类聚合平台的**余额接口不接受模型密钥**（sk-…），
    它认的是账号后台的「系统访问令牌 + 用户 ID」。把这两个值塞进 api_key 会
    直接把模型调用搞坏。其他站点留空即可 —— 空字典走原来的三层降级，行为不变。
    '''

    if not isinstance(a, dict):
        return {}
    out = {}
    tok = str(a.get('system_token') or '').strip()
    uid = str(a.get('user_id') or '').strip()
    if tok:
        out['system_token'] = tok[:256]
    if uid:
        out['user_id'] = uid[:64]
    return out

def _norm_body(b):
    '''请求体附加参数的收口：只收扁平/浅层 dict，拒掉超长与非法 key。

    不做白名单校验 —— 各家字段名只有用户/文档知道。但要挡住两类脏数据：
    字符串（用户误填）与超深嵌套（无意义且会污染日志）。
    '''

    if not isinstance(b, dict):
        return {}
    out = {}
    for k, v in b.items():
        k = str(k or '').strip()
        if k:
            if len(k) > 64 or k.startswith('_'):
                continue
            if isinstance(v, (dict, list)):
                if len(json.dumps(v, ensure_ascii=False)) > 512:
                    continue
            elif isinstance(v, str):
                if len(v) > 256:
                    continue
        out[k] = v
    return out

def _for_storage(it):
    keep = ('id', 'name', 'protocol', 'base_url', 'api_key', 'models', 'headers', 'vision', 'enabled', 'ctx_window', 'note', 'balance_manual', 'balance_auth', 'extra_body', 'created_at', 'updated_at')
    return {k: it.get(k) for k in keep if it.get(k) is not None if k == 'api_key'}

def mask_key(key):
    '''给前端看的脱敏形态：只留头尾，够用户确认是哪一个 key。'''

    k = str(key or '')
    if not k:
        return ''
    if len(k) <= 8:
        return '•' * len(k)
    return f'{k[:4]}{'••••••'}{k[-4:]}'

def slug_from_url(url):
    '''从地址推一个 ASCII slug：api.siliconflow.cn → siliconflow，
    tokenrhythm.studio → tokenrhythm，127.0.0.1:1234 → local-1234。'''

    s = str(url or '').strip()
    if not s:
        return ''
    if not _SCHEME_RE.match(s):
        s = 'https://' + s
    m = _URL_RE.match(s)
    host = (m.group('host') if m else '').split('@')[-1]
    port = ''
    if ':' in host and not host.startswith('['):
        host, _, port = host.partition(':')
    def _with_port(base):
        if port:
            return f'{base}-{port}'
        return base

    if re.fullmatch('\\d{1,3}(\\.\\d{1,3}){3}', host) or host.startswith('['):
        return _with_port('local')
    host = host.lower().lstrip('.')
    labels = [x for x in host.split('.') if x]
    if labels and labels[0] in ('api', 'www', 'open', 'gateway', 'llm', 'ai'):
        labels = labels[1:]
    if not labels:
        return _with_port('provider')
    slug = labels[0]
    slug = re.sub('[^a-z0-9\\-]+', '-', slug).strip('-')
    if slug in ('localhost',):
        return _with_port('local')
    if slug:
        return _with_port(slug)
    return _with_port('provider')

def make_id(root, url, name=''):
    '''生成唯一 id（避开内置 provider 与已有自定义源）。'''

    base = slug_from_url(url)
    if not base or base in BUILTIN_IDS:
        base = base or 'provider'
        base = f'{base}-custom' if base in BUILTIN_IDS else base
    if base in BUILTIN_IDS:
        base = 'custom-' + base
    existing = {i['id'] for i in load_all(root)}
    if base not in existing:
        return base
    n = 2
    while f'{base}-{n}' in existing:
        n += 1
    return f'{base}-{n}'

def public_list(root):
    '''给前端的列表：密钥脱敏，附归一化地址与模型数。'''

    out = []
    for it in load_all(root):
        out.append({'id': it['id'], 'name': it['name'], 'protocol': it['protocol'], 'base_url': it['base_url'], 'api_base': it['api_base'], 'key_masked': mask_key(it['api_key']), 'has_key': bool(it['api_key']), 'models': [{'id': m['id'], 'name': m['id']} for m in it['models'] if m.get('name')], 'model_count': len(it['models']), 'headers': it['headers'], 'vision': it['vision'], 'enabled': it['enabled'], 'ctx_window': it['ctx_window'], 'note': it['note'], 'balance_manual': it.get('balance_manual') or {}, 'balance_auth': _public_bal_auth(it.get('balance_auth')), 'extra_body': it.get('extra_body') or {}, 'updated_at': it['updated_at']})
        continue
    return out

def normalize_base_url(url, protocol='openai'):
    '''把用户粘进来的各种形态收敛成「API 根」。

规则（顺序敏感）：
  1. 没写协议 → 补 https://（本地地址会自动纠成 http://）
  2. 去掉 query / fragment / 尾部斜杠
  3. 剥掉整段端点后缀（/chat/completions、/messages …）
  4. 路径为空 → 补 /v1（绝大多数 OpenAI 兼容站点的惯例）
     路径已存在（/api/paas/v4、/openai/v1 …）→ 原样保留
'''

    s = str(url or '').strip().strip('"').strip("'").strip()
    if not s:
        return ''
    if not _SCHEME_RE.match(s):
        host_part = s.split('/')[0].split(':')[0].lower()
        local = host_part in ('localhost', '127.0.0.1', '0.0.0.0', '[::1]') or host_part.endswith('.local') or host_part.startswith('192.168.') or host_part.startswith('10.') or host_part.startswith('172.')
        s = ('http://' if local else 'https://') + s
    s = s.split('#')[0].split('?')[0].strip()
    m = _URL_RE.match(s)
    if not m:
        return s.rstrip('/')
    scheme = m.group('scheme').lower()
    host = m.group('host')
    path = (m.group('path') or '').rstrip('/')
    low = path.lower()
    for suf in sorted(_ENDPOINT_SUFFIXES, key=len, reverse=True):
        if not low.endswith(suf):
            continue
        path = path[:-len(suf)].rstrip('/')
        break
    if path in ('', '/'):
        path = '/v1'
    return scheme + host + path

def endpoint_candidates(base_url, endpoint, protocol='openai'):
    '''给出该 base 下值得一试的候选 URL（按优先级）。

    目的是容错：站点可能把 API 挂在根、/v1、或自定义路径下。
    '''

    base = normalize_base_url(base_url, protocol)
    if not base:
        return []
    out = [base + endpoint]
    if not base.rstrip('/').endswith('/v1'):
        out.append(base + '/v1' + endpoint)
    m = _URL_RE.match(base)
    if m:
        root = m.group('scheme').lower() + m.group('host')
        if base.rstrip('/') != root + '/v1':
            out.append(root + '/v1' + endpoint)
        if base.rstrip('/') != root:
            out.append(root + endpoint)
    seen, uniq = set(), []
    for u in out:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq

_SSL_CTX = None

def _ssl_ctx():
    '''与 llm.py 同款：本地部署环境常被注入自签证书，禁用校验可接受。'''

    global _SSL_CTX
    if _SSL_CTX is None:
        import ssl
        c = ssl.create_default_context()
        c.check_hostname = False
        c.verify_mode = ssl.CERT_NONE
        _SSL_CTX = c
    return _SSL_CTX

def _short_err(e):
    '''把 urllib 的异常压成一句人话。'''

    if isinstance(e, urllib.error.HTTPError):
        body = ''
        try:
            body = e.read().decode('utf-8', 'replace')[:300]
        except Exception:
            pass
        if body:
            return f'HTTP {e.code}' + f': {body}'
        return None + ''
    if isinstance(e, urllib.error.URLError):
        return f'连接失败: {getattr(e, 'reason', e)}'
    return str(e)[:300]

def _headers_for(api_key, protocol, extra=None):
    h = {'Content-Type': 'application/json', 'Accept': 'application/json'}
    key = str(api_key or '').strip()
    if key:
        h['Authorization'] = f'Bearer {key}'
        if protocol == 'anthropic':
            h['x-api-key'] = key
    if protocol == 'anthropic':
        h['anthropic-version'] = '2023-06-01'
        h.setdefault('Accept', 'application/json')
    if extra:
        for k, v in extra.items():
            if k:
                if v is not None:
                    h[str(k)] = str(v)
    return h

def _request(method, url, api_key, protocol, extra_headers, body=None, timeout=15):
    '''返回 (status, json_or_text, error_str)。status=0 表示压根没连上。'''

    data = json.dumps(body).encode('utf-8') if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=_headers_for(api_key, protocol, extra_headers))
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx()) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        return e.code, '', _short_err(e)
    except Exception as e:
        return 0, '', _short_err(e)
    try:
        pass
    except Exception:
        try:
            return resp.status, raw.decode('utf-8', 'replace')[:500], ''
        except urllib.error.HTTPError as e:
            return e.code, '', _short_err(e)
        except Exception as e:
            return 0, '', _short_err(e)
    return resp.status, json.loads(raw.decode('utf-8', 'replace')), ''

def _parse_models(payload):
    '''从各家 /models 响应里挖出模型 id（OpenAI 的 data[] 为主，兼容几种变体）。'''

    out = []
    if isinstance(payload, dict):
        arr = payload.get('data')
        if arr is None:
            arr = payload.get('models')
        if isinstance(arr, dict):
            arr = [{'id': k, **(v if isinstance(v, dict) else {})} for k, v in arr.items()]
        if isinstance(arr, list):
            for m in arr:
                if isinstance(m, dict):
                    mid = m.get('id') or m.get('name') or m.get('model')
                else:
                    mid = m
                if mid:
                    out.append({'id': str(mid).strip(), 'name': ''})
        return out
    if isinstance(payload, list):
        for m in payload:
            mid = m.get('id') if isinstance(m, dict) else m
            if mid:
                out.append({'id': str(mid).strip(), 'name': ''})
    return out

def fetch_models(base_url, api_key='', protocol='openai', extra_headers=None, timeout=15):
    '''只拉模型列表。返回 (models, 详情)。详情含每个候选端点的尝试结果。'''

    detail = {'tried': []}
    for url in endpoint_candidates(base_url, '/models', protocol):
        t0 = time.time()
        status, payload, err = _request('GET', url, api_key, protocol, extra_headers, timeout=timeout)
        ms = int((time.time() - t0) * 1000)
        detail['tried'].append({'url': url, 'status': status, 'ms': ms, 'note': err or ('ok' if status == 200 else '')})
        if status == 200:
            models = _parse_models(payload)
            if models:
                detail.update({'ok': True, 'url': url, 'ms': ms})
                return models, detail
            detail['tried'][-1]['note'] = '端点可达但没有解析出模型'
        if status not in (401, 403):
            continue
        detail['auth_error'] = err or f'HTTP {status}'
    return [], detail

_PING_TEXT = 'ping'
_NON_CHAT_HINT = ('tts', 'asr', 'audio', 'whisper', 'speech', 'voice', 'realtime', 'embed', 'rerank', 'music', 'image', 'video', 'ocr', 'search')

def probe_model_candidates(models, explicit='', limit=4):
    '''给探活挑几个"最像能对话"的模型名，按优先级排序。

    用户显式指定模型就只认它（不做兜底，避免"测试通过但用户填的是错的"）；
    否则从列表里剔除明显的非对话模型，剩下按原顺序取前 `limit` 个。
    '''

    name = str(explicit or '').strip()
    if name:
        return [name]
    ids = []
    for m in models or []:
        mid = (m.get('id') if isinstance(m, dict) else str(m)) or ''
        mid = str(mid).strip()
        if not mid:
            continue
        if mid not in ids:
            ids.append(mid)
    other = chat = []
    for mid in ids:
        low = mid.lower()
        (other if any((h in low for h in _NON_CHAT_HINT)) else chat).append(mid)
    return (chat + other)[:max(1, int(limit or 1))]

def _looks_like_model_issue(status, err):
    '''失败是不是"接口通、只是模型名不被认"？

    ⚠️ 不能只认 400：阶跃星辰对无效模型回的是 **404** + `type=model_invalid`，
    照"404 = 路径不对"处理就会把它误判成"站点没有对话接口"。
    判定必须落在**响应正文提到模型**上，不能只看状态码 —— 路径写错时也会 404，
    但那种响应里不会出现模型名。
    '''

    if status not in (400, 404, 422):
        return False
    low = str(err or '').lower()
    if not low:
        return False
    if 'model_invalid' in low or 'invalid_model' in low:
        return True
    if 'model' not in low:
        return False
    return any((k in low for k in ('does not exist', 'not exist', 'not found', 'unknown', 'unsupported', 'not supported', 'no access', 'do not have access', 'invalid')))

def _probe_openai_chat(base, api_key, extra_headers, timeout, models=None):
    '''最小 OpenAI chat 探活 → dict（ok/status/err/echo/hit/model/model_issue）。

    有些站点不实现 /v1/models，但对话接口是好的 —— 不能因为没列表就判死。
    **必须回报命中的 URL**：候选里有 xxx/chat/completions 与 xxx/v1/chat/completions
    多个形态，只有命中的那个才能反推出可用的 API 根（否则会把 /v1 丢掉，
    测试显示"通过"、真实对话却 404）。

    逐个换模型试：遇到"模型名无效"就地换下一个候选，而不是直接判端点不通。
    '''

    urls = endpoint_candidates(base, '/chat/completions', 'openai')
    cands = [str(m).strip() for m in models or [] if not m if str('').strip()] or ['']
    res = {'ok': False, 'status': 0, 'err': '未尝试', 'echo': '', 'hit': '', 'model': '', 'model_issue': False}
    for mid in cands:
        body = {'model': mid or 'gpt-3.5-turbo', 'messages': [{'role': 'user', 'content': _PING_TEXT}], 'max_tokens': 16, 'stream': False}
        for url in urls:
            status, payload, err = _request('POST', url, api_key, 'openai', extra_headers, body=body, timeout=timeout)
            if status == 200:
                echo = ''
                try:
                    echo = payload['choices'][0]['message'].get('content', '')[:40]
                except Exception:
                    pass
                return {'ok': True, 'status': 200, 'err': '', 'echo': echo, 'hit': url, 'model': mid, 'model_issue': False}
            res.update({'status': status, 'err': err, 'hit': url})
            if _looks_like_model_issue(status, err):
                res['model_issue'] = True
                break
            if not status in (401, 403):
                continue
            return res
    return res

def _probe_anthropic(base, api_key, extra_headers, timeout, models=None):
    '''最小 Anthropic /v1/messages 探活 → dict（字段同 _probe_openai_chat）。'''

    urls = endpoint_candidates(base, '/messages', 'anthropic')
    cands = [str(m).strip() for m in models or [] if not m if str('').strip()] or ['']
    res = {'ok': False, 'status': 0, 'err': '未尝试', 'echo': '', 'hit': '', 'model': '', 'model_issue': False}
    for mid in cands:
        body = {'model': mid or 'claude-3-5-sonnet-latest', 'max_tokens': 16, 'messages': [{'role': 'user', 'content': _PING_TEXT}]}
        for url in urls:
            status, payload, err = _request('POST', url, api_key, 'anthropic', extra_headers, body=body, timeout=timeout)
            if status == 200:
                echo = ''
                try:
                    for b in payload.get('content') or []:
                        if b.get('type') != 'text':
                            continue
                        echo = (b.get('text') or '')[:40]
                        break
                except Exception:
                    pass
                return {'ok': True, 'status': 200, 'err': '', 'echo': echo, 'hit': url, 'model': mid, 'model_issue': False}
            res.update({'status': status, 'err': err, 'hit': url})
            if _looks_like_model_issue(status, err):
                res['model_issue'] = True
                break
            if not status in (401, 403):
                continue
            return res
    return res

def probe(base_url, api_key='', protocol='auto', extra_headers=None, timeout=15, probe_chat=True, model=''):
    '''探测一个自定义源：能不能连、是什么协议、有哪些模型。

protocol: "auto" 自动识别 / "openai" / "anthropic"

为什么不能只靠 /models 判协议：很多站点照样提供 /v1/models（列表），
却只实现了 /v1/messages（或反过来）。只看到列表就下结论会把协议认错，
表现为"测试通过，一发消息就 404"。所以：**列表负责拿模型名，对话探活
负责定协议**，两步都过才叫通。

返回 dict（前端直接展示）：
  ok, protocol, detected_protocol, protocol_mismatch, api_base, models,
  latency_ms, auth_error, warning, error, advice, tried[]
'''

    raw_base = str(base_url or '').strip()
    started = time.time()
    result = {'ok': False, 'protocol': 'openai', 'detected_protocol': '', 'protocol_mismatch': False, 'base_url': raw_base, 'api_base': '', 'models': [], 'models_url': '', 'latency_ms': 0, 'auth_error': '', 'warning': '', 'error': '', 'advice': '', 'tried': []}
    if not raw_base:
        result['error'] = '请填写 Base URL'
        return result
    want = str(protocol).lower()
    order = ['anthropic', 'openai'] if want == 'anthropic' else ['openai', 'anthropic']
    api_base = normalize_base_url(raw_base, order[0])
    result['api_base'] = api_base
    def _finish(proto, base, warning=''):
        result.update({'ok': True, 'protocol': proto, 'detected_protocol': proto, 'api_base': base, 'warning': warning, 'latency_ms': int((time.time() - started) * 1000)})
        result['protocol_mismatch'] = bool(want in PROTOCOLS and want != proto)
        if result['protocol_mismatch']:
            extra = f'该端点实际是「{'Anthropic' if proto == 'anthropic' else 'OpenAI'} 协议」，已自动识别。'
            result['advice'] = (extra + ' ' + warning).strip()
        return result

    models, mdetail = fetch_models(api_base, api_key, order[0], extra_headers, timeout=timeout)
    result['tried'].extend([{**t, 'proto': order[0], 'kind': 'models'} for t in mdetail.get('tried', [])])
    if not models:
        if mdetail.get('auth_error') and api_key and str(api_key).strip():
            m2, d2 = fetch_models(api_base, api_key, order[1], extra_headers, timeout=timeout)
            result['tried'].extend([{**t, 'proto': order[1], 'kind': 'models'} for t in d2.get('tried', [])])
            if m2:
                models, mdetail = m2, d2
    result['models'] = models
    result['models_url'] = mdetail.get('url', '')
    if mdetail.get('auth_error'):
        result['auth_error'] = mdetail['auth_error']
    _mu = mdetail.get('url') or ''
    if models and _mu.endswith('/models'):
        api_base = _mu[:-len('/models')].rstrip('/')
        result['api_base'] = api_base
    if not probe_chat:
        if models:
            return _finish(order[0], api_base)
        result['latency_ms'] = int((time.time() - started) * 1000)
        result['error'] = result['auth_error'] and f'鉴权失败（{result['auth_error']}）' or '拿不到模型列表'
        result['advice'] = '打开「对话探活」可以进一步确认协议与鉴权。'
        return result
    probe_models = probe_model_candidates(models, model)
    partial = None
    for proto in order:
        base = api_base or normalize_base_url(raw_base, proto)
        if proto == 'openai':
            res = _probe_openai_chat(base, api_key, extra_headers, timeout, probe_models)
            suffix = '/chat/completions'
        else:
            res = _probe_anthropic(base, api_key, extra_headers, timeout, probe_models)
            suffix = '/messages'
        ok, status, err = res['ok'], res['status'], res['err']
        echo, hit = res['echo'], res['hit']
        fallback_mark = base + suffix
        if ok and hit and hit.endswith(suffix):
            base = hit[:-len(suffix)].rstrip('/')
        mark = hit if ok and hit else fallback_mark
        if ok:
            note = f'探活成功（模型 {res['model'] or '默认'} 回显: {echo or '空'}）'
        else:
            note = err or f'HTTP {status}'
        if not ok and res['model_issue']:
            partial = proto
            note = '接口已通（候选模型名都不被接受，请在下方选一个正确的模型 ID）：' + note
        elif not ok and status == 400:
            if 'model' in (err or '').lower():
                partial = proto
                note = '接口已通（模型名不被接受，请在下方补上正确的模型 ID）：' + note
        result['tried'].append({'url': mark, 'status': status, 'ms': 0, 'note': note, 'proto': proto, 'kind': 'chat'})
        if ok:
            return _finish(proto, base)
        if partial:
            return _finish(proto, base, warning='对话接口已通、鉴权有效，但默认模型名被拒 —— 请确认模型 ID 正确。')
        if status not in (401, 403):
            continue
        result['auth_error'] = err or f'HTTP {status}'
        continue
    result['latency_ms'] = int((time.time() - started) * 1000)
    st = [t for t in result['tried'] if t.get('status')]
    codes = {t['status'] for t in st}
    if models:
        result['error'] = '能拉到模型列表，但对话接口没通'
        result['advice'] = '地址可能只提供模型目录，或 API 根路径不对；核对站点文档里的对话端点（多数是 /v1/chat/completions）。'
    elif result['auth_error']:
        result['error'] = f'鉴权失败（{result['auth_error']}）'
        result['advice'] = '地址是通的，但没通过校验 —— 请检查 API Key 是否完整、是否已过期。'
    elif 404 in codes or 405 in codes:
        result['error'] = '没找到对话接口（/chat/completions 与 /messages 都返回 404/405）'
        result['advice'] = '请确认 Base URL 填的是 API 根地址。多数站点形如 https://站点域名/v1 ；也有站点用 /api/v1 或 /openai/v1。'
    elif codes and max(codes) >= 500:
        result['error'] = '对方服务端报错（5xx），稍后重试或换个地址'
    elif 0 in codes or not st:
        first = next((t for t in result['tried'] if t.get('note')), None)
        result['error'] = '连不上：' + (first['note'] if first else '网络不可达')
        result['advice'] = '检查地址拼写、本机网络/代理，以及该站点是否只对部分地区开放。'
    else:
        result['error'] = '探测失败（未知响应）'
    result['protocol'] = want if want in PROTOCOLS else 'openai'
    return result

def runtime_config(root, provider_id):
    '''取一个源的运行时字段，交给 LLMConfig：base_url / api_key / protocol / headers。

    llm.py 只认 base_url（会自动拼 /chat/completions），所以这里给 **api_base**
    而不是用户填的原始 base_url —— 用户可能填了带端点的长地址。
    '''

    it = get(root, provider_id)
    if not it:
        return
    return {'base_url': it['api_base'] or normalize_base_url(it['base_url'], it['protocol']), 'api_key': it['api_key'], 'protocol': it['protocol'], 'extra_headers': it['headers'], 'extra_body': it.get('extra_body') or {}, 'ctx_window': it['ctx_window'], 'vision': it['vision'], 'name': it['name']}

def fingerprint(url):
    '''调试用：给地址算个短指纹（日志里代替完整地址，避免泄露 key 在 URL 里的站点）。'''

    return hashlib.sha1(str(url or '').encode('utf-8')).hexdigest()[:8]

CURRENCY_SYMBOL = {'CNY': '¥', 'RMB': '¥', 'USD': '$', 'USDT': '$', 'EUR': '€', 'JPY': '¥', 'HKD': 'HK$', 'GBP': '£', 'TWD': 'NT$'}

def currency_symbol(code):
    return CURRENCY_SYMBOL.get(str(code or '').strip().upper(), '')

def origin_of(url):
    '''取 scheme://host:port（丢掉路径）。'''

    s = str(url or '').strip()
    if not s:
        return ''
    if not _SCHEME_RE.match(s):
        s = 'https://' + s
    p = urllib.parse.urlsplit(s)
    return urllib.parse.urlunsplit((p.scheme, p.netloc, '', '', ''))

def _qp(body, path):
    '''按 "a.b.0.c" 点路径取值（数字段索引数组）。取不到返回 None。'''

    if not path:
        return
    cur = body
    for seg in str(path).split('.'):
        if isinstance(cur, dict):
            cur = cur.get(seg)
        elif isinstance(cur, list):
            try:
                cur = cur[int(seg)]
            except Exception:
                return
        else:
            return
        if cur is None:
            return
    return cur

def _qnum(v):
    """宽松数值：'88.88' / '¥88.88' / 88.88 → 88.88；布尔与非数字 → None。"""

    if v is None or isinstance(v, bool):
        return
    if isinstance(v, (int, float)):
        return float(v)
    try:
        s = str(v).strip().replace(',', '')
        s = re.sub('^[^\\d\\-.]+', '', s)
        if s:
            return float(s)
    except Exception:
        return

QUOTA_VENDORS = {'label': 'DeepSeek', 'hosts': ('api.deepseek.com',), 'path': '/user/balance', 'remaining': 'balance_infos.0.total_balance', 'unit': 'balance_infos.0.currency', 'detail': {'充值余额': 'balance_infos.0.topped_up_balance', '赠送余额': 'balance_infos.0.granted_balance'}}, {'label': '硅基流动', 'hosts': ('api.siliconflow.cn',), 'path': '/v1/user/info', 'remaining': 'data.totalBalance', 'unit_fixed': 'CNY', 'detail': {'充值余额': 'data.chargeBalance', '赠送余额': 'data.balance'}}, {'label': '硅基流动', 'hosts': ('api.siliconflow.com',), 'path': '/v1/user/info', 'remaining': 'data.totalBalance', 'unit_fixed': 'USD', 'detail': {'充值余额': 'data.chargeBalance', '赠送余额': 'data.balance'}}, {'label': 'Kimi (月之暗面)', 'hosts': ('api.moonshot.cn', 'api.moonshot.ai', 'api.kimi.ai', 'api.kimi.com'), 'path': '/v1/users/me/balance', 'remaining': 'data.available_balance', 'unit_fixed': 'CNY', 'detail': {'现金余额': 'data.cash_balance', '赠送余额': 'data.voucher_balance'}}, {'label': '阶跃星辰', 'hosts': ('api.stepfun.com', 'api.stepfun.ai'), 'path': '/v1/accounts', 'remaining': 'balance', 'unit_fixed': 'CNY', 'detail': {'累计充值': 'total_cash_balance', '累计赠送': 'total_voucher_balance'}}, {'label': 'OpenRouter', 'hosts': ('openrouter.ai',), 'path': '/api/v1/credits', 'remaining': None, 'minus': ('data.total_credits', 'data.total_usage'), 'total': 'data.total_credits', 'used': 'data.total_usage', 'unit_fixed': 'USD'}, {'label': 'Novita AI', 'hosts': ('api.novita.ai',), 'path': '/v3/user/balance', 'remaining': 'availableBalance', 'scale': 10000.0, 'unit_fixed': 'USD'}, {'label': '智谱 BigModel', 'hosts': ('open.bigmodel.cn',), 'path': '/api/biz/account/query-customer-account-report', 'remaining': 'data.balance', 'unit_fixed': 'CNY', 'require': 'success', 'private': True}, {'label': 'DMXAPI', 'hosts': ('dmxapi.cn', 'dmxapi.com'), 'engine': 'dmx', 'path': '/api/user/self', 'remaining': 'data.quota', 'used': 'data.used_quota', 'scale': 500000.0, 'unit_fixed': 'CNY', 'require': 'success', 'auth_hint': '个人资料 → 安全 → 访问令牌（系统访问令牌）＋个人资料页顶部的用户 ID'}
_LOOSE_KEYS = {'left', 'quota', 'credit', 'balance', 'credits', 'surplus', 'available', 'remaining', 'totalbalance', 'creditbalance', 'walletbalance', 'accountbalance', 'availablequota', 'remainingquota', 'availablecredit', 'remainingcredit', 'availablebalance', 'balanceavailable', 'remainingbalance'}
_LOOSE_PREF = ('remaining', 'remainingbalance', 'availablebalance', 'available', 'balance', 'totalbalance', 'creditbalance', 'credits', 'remainingquota', 'quota', 'left', 'surplus')

def _norm_key(k):
    return re.sub('[^a-z0-9]+', '', str(k or '').lower())

def _get_json(url, api_key, protocol, extra_headers, timeout, tried):
    '''GET 一个 JSON 端点，并把尝试记进 tried（前端要展示探测过程）。'''

    t0 = time.time()
    st, payload, err = _request('GET', url, api_key, protocol, extra_headers, timeout=timeout)
    ms = int((time.time() - t0) * 1000)
    note = err or ('ok' if st == 200 else '')
    if st == 200 and not isinstance(payload, (dict, list)):
        note = '非 JSON 响应（可能是网页）'
    tried.append({'url': url, 'status': st, 'ms': ms, 'note': str(note)[:160]})
    return st, payload, err

def _quota_vendor_for(base_url):
    '''按域名匹配已知厂商，返回 (厂商条目, host)。'''

    host = ''
    try:
        s = str(base_url or '').strip()
        if not _SCHEME_RE.match(s):
            s = 'https://' + s
        host = (urllib.parse.urlsplit(s).hostname or '').lower()
    except Exception:
        host = ''
    if not host:
        return None, host
    for v in QUOTA_VENDORS:
        for h in v['hosts']:
            if host == h or host.endswith('.' + h):
                return v, host
    return None, host

def _quota_vendor(v, base_url, api_key, protocol, extra_headers, timeout, tried):
    url = origin_of(base_url) + v['path']
    st, payload, err = _get_json(url, api_key, protocol, extra_headers, timeout, tried)
    if st != 200 or not isinstance(payload, dict):
        return
    if v.get('require') and not _qp(payload, v['require']):
        return
    rem = None
    if v.get('minus'):
        a, b = v['minus']
        va, vb = _qnum(_qp(payload, a)), _qnum(_qp(payload, b))
        if va is not None:
            rem = va - (vb or 0.0)
    else:
        rem = _qnum(_qp(payload, v.get('remaining')))
    if rem is None:
        return
    if v.get('scale'):
        rem = rem / float(v['scale'])
    cur = _qp(payload, v['unit']) if v.get('unit') else None
    cur = str(cur or v.get('unit_fixed') or '').strip().upper()
    out = {'remaining': round(float(rem), 4), 'currency': cur, 'endpoint': url, 'provider': v['label'], 'raw': json.dumps(payload, ensure_ascii=False)[:400]}
    tot = _qnum(_qp(payload, v.get('total'))) if v.get('total') else None
    used = _qnum(_qp(payload, v.get('used'))) if v.get('used') else None
    if tot is not None:
        out['total'] = round(tot, 4)
    if used is not None:
        out['used'] = round(used, 4)
    det = {}
    for label, path in (v.get('detail') or {}).items():
        val = _qp(payload, path)
        if val not in (None, ''):
            det[label] = val
    if det:
        out['detail'] = det
    return out

def _quota_dmx(v, base_url, api_key, balance_auth, timeout, tried):
    '''DMXAPI 专用适配器：用「系统访问令牌 + 用户 ID」查账号余额。

    为什么不复用 _quota_vendor：那条路的认证是 API Key（Bearer），而 DMX 的
    余额接口要求另外两个凭据 —— 模型密钥发过去只会吃 401。官方口径见
    https://doc.dmxapi.cn/yuer.html：余额 = data.quota ÷ 500000 元。

    返回 None 表示"这次没查到"（缺凭据 / 凭据被拒 / 结构变了），由调用方解释。
    '''

    auth = balance_auth if isinstance(balance_auth, dict) else {}
    tok = str(auth.get('system_token') or '').strip()
    uid = str(auth.get('user_id') or '').strip()
    if not tok or not uid:
        return
    url = origin_of(base_url) + v['path']
    hdrs = {'Authorization': 'Bearer ' + tok, 'Dmx-Api-User': uid}
    t0 = time.time()
    try:
        st, payload, err = _request('GET', url, '', 'openai', hdrs, timeout=timeout)
    except Exception as e:
        st, payload, err = 0, '', _short_err(e)
    tried.append({'url': url, 'status': st, 'ms': int((time.time() - t0) * 1000), 'note': str(err or ('ok' if st == 200 else ''))[:160]})
    if st != 200 or not isinstance(payload, dict):
        return
    if v.get('require') and not _qp(payload, v['require']):
        return
    rem = _qnum(_qp(payload, v.get('remaining')))
    if rem is None:
        rem = _qnum(_qp(payload, 'data.balance'))
    if rem is None:
        return
    scale = float(v.get('scale') or 1.0)
    out = {'remaining': round(rem / scale, 4), 'currency': str(v.get('unit_fixed') or 'CNY'), 'endpoint': url, 'provider': v['label'], 'raw': json.dumps(payload, ensure_ascii=False)[:400]}
    used = _qnum(_qp(payload, v.get('used'))) if v.get('used') else None
    if used is not None:
        out['used'] = round(used / scale, 4)
        out['total'] = round((rem + used) / scale, 4)
    det = {'账户原始额度': rem}
    if used is not None:
        det['已用原始额度'] = used
    out['detail'] = det
    return out

def _quota_relay(base_url, api_key, protocol, extra_headers, timeout, tried):
    '''one-api / new-api 系中转站兜底。

    返回 None（不是这一系）或 dict。dict 里可能带 _auth / _unlimited 标记，
    由调用方翻译成人话 —— 这两种情况比「查不到」有价值得多。
    '''

    roots = []
    for r in origin_of(base_url), str(base_url or '').rstrip('/'):
        if not r:
            continue
        if r not in roots:
            roots.append(r)
    sub, sub_url = None, ''
    for root in roots:
        url = root + '/dashboard/billing/subscription'
        st, payload, err = _get_json(url, api_key, protocol, extra_headers, timeout, tried)
        if st == 200 and isinstance(payload, dict):
            if 'hard_limit_usd' in payload or 'soft_limit_usd' in payload:
                sub, sub_url = payload, url
                break
        if st not in (401, 403):
            continue
        return {'_auth': True, 'endpoint': url, 'error': err or f'HTTP {st}'}
    if sub is None:
        return
    hard = _qnum(sub.get('hard_limit_usd'))
    if hard is None:
        hard = _qnum(sub.get('soft_limit_usd'))
    if hard is None:
        return
    if hard >= 99999000.0:
        return {'_unlimited': True, 'endpoint': sub_url, 'raw': json.dumps(sub, ensure_ascii=False)[:400]}
    today = time.strftime('%Y-%m-%d')
    total_usage, used_url = None, ''
    for root in roots:
        url = root + '/dashboard/billing/usage'
        st, payload, err = _get_json(url + '?start_date=2020-01-01&end_date=' + today, api_key, protocol, extra_headers, timeout, tried)
        if st != 200:
            continue
        if not isinstance(payload, dict):
            continue
        if not payload.get('total_usage') is not None:
            continue
        total_usage = _qnum(payload.get('total_usage'))
        used_url = url
        break
    out = {'endpoint': sub_url, 'provider': 'one-api / new-api 中转', 'currency': 'USD', 'total': round(hard, 4), 'raw': json.dumps(sub, ensure_ascii=False)[:400]}
    if total_usage is None:
        out['advice'] = '站点没返回已用量，这里只能显示总额度'
        return out
    out['used'] = round(total_usage / 100.0, 4)
    out['remaining'] = round(hard - total_usage / 100.0, 4)
    out['detail'] = {'原始 total_usage（美分）': total_usage}
    return out

def _loose_scan(body, depth=0, path=''):
    '''在 JSON 里找像「余额」的标量。只下探两层，不进数组。'''

    out = []
    if not isinstance(body, dict) or depth > 2:
        return out
    for k, v in body.items():
        p = f'{path}.{k}' if path else str(k)
        if isinstance(v, dict):
            out += _loose_scan(v, depth + 1, p)
        else:
            if isinstance(v, list):
                continue
            if _norm_key(k) in _LOOSE_KEYS:
                n = _qnum(v)
                if n is not None:
                    out.append({'path': p, 'key': _norm_key(k), 'value': n})
    return out

_LOOSE_PATHS = ('/v1/user/balance', '/user/balance', '/v1/user/info', '/v1/account/balance', '/account/balance', '/v1/balance', '/balance', '/v1/credits', '/v1/quota', '/v1/me', '/v1/dashboard/billing/subscription', '/api/user/self', '/usage')
_LOOSE_PATHS_BASE = ('/user/balance', '/user/info', '/me', '/balance', '/usage')

def _get_json_quiet(url, api_key, protocol, extra_headers, timeout):
    '''并发探测用：不发 tried（38 条日志没人看），只回结果。'''

    t0 = time.time()
    st, payload, err = _request('GET', url, api_key, protocol, extra_headers, timeout=timeout)
    return st, payload, err, int((time.time() - t0) * 1000)

def _quota_loose(base_url, api_key, protocol, extra_headers, timeout, tried):
    '''宽松识别：并发抓常见路径，找像余额的数字字段。

    为什么要并发：串行试完这些路径要 5~6 秒，用户点一下"查余额"等到怀疑人生。
    为什么要精简路径：对一个陌生站点发几十个请求既慢又容易触发风控，
    宁可覆盖面小一点 —— 覆盖不到的让用户手填，比偷偷打人家站点好。
    '''

    from concurrent.futures import ThreadPoolExecutor
    base = str(base_url or '').rstrip('/')
    origin = origin_of(base_url)
    urls = [origin + p for p in _LOOSE_PATHS]
    if base and base != origin:
        urls += [base + p for p in _LOOSE_PATHS_BASE]
    found = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(_get_json_quiet, u, api_key, protocol, extra_headers, timeout): u for u in urls}
        for fut in futs:
            try:
                st, payload, _err, _ms = fut.result()
            except Exception:
                continue
            if st != 200:
                continue
            if isinstance(payload, dict):
                hits = _loose_scan(payload)
                if hits:
                    found.append((futs[fut], payload, hits))
    if not found:
        return
    found.sort(key=(lambda f: 99))
    url, payload, hits = found[0]
    hits.sort(key=(lambda h: 99))
    best = hits[0]
    return {'remaining': round(float(best['value']), 4), 'currency': '', 'endpoint': url, 'provider': '', 'detail': {h['path']: h['value'] for h in hits[:5]}, 'raw': json.dumps(payload, ensure_ascii=False)[:400], 'identify': best['path'], 'probed': len(urls)}

def query_balance(base_url, api_key='', protocol='openai', extra_headers=None, timeout=10, depth='full', loose=None, balance_auth=None):
    '''查一个源的余额 / 额度。三层降级，详见本段顶部注释。

balance_auth 是「平台管理凭据」{"system_token","user_id"}：只被需要它的
厂商用（目前只有 DMXAPI —— 它的余额接口不认模型密钥）。不传 = 行为跟以前
完全一样，其他厂商一律不看这个参数。

depth 控制查到多深（请求量差很多，别乱用 full）：
  "vendor" —— 只查已知厂商表（1 个请求）
  "relay"  —— 再加 one-api / new-api 兜底（最多 4 个请求）
  "full"   —— 再加宽松识别（最多 ~22 个请求，**只在用户手动点"查余额"时用**）
后台每 120s 轮询顶栏余额时必须用 "relay"：对自研站点的 full 探测
等于每两分钟打人家二十几个请求，很不礼貌。

返回 dict：
  ok/kind(vendor|relay|auto|none)/provider/remaining/currency/symbol/
  total/used/detail/endpoint/raw/error/advice/tried
'''

    if loose is False:
        depth = 'vendor'
    elif loose is True:
        depth = 'full'
    if depth not in ('vendor', 'relay', 'full'):
        depth = 'full'
    tried = []
    out = {'ok': False, 'kind': 'none', 'provider': '', 'remaining': None, 'currency': '', 'symbol': '', 'total': None, 'used': None, 'detail': {}, 'endpoint': '', 'raw': '', 'error': '', 'advice': '', 'tried': tried, 'base_url': base_url}
    if not str(base_url or '').strip():
        out['error'] = '缺少 Base URL'
        return out
    v, host = _quota_vendor_for(base_url)
    if v:
        if v.get('engine') == 'dmx':
            try:
                r = _quota_dmx(v, base_url, api_key, balance_auth, timeout, tried)
            except Exception as e:
                r = None
                out['error'] = _short_err(e)
            if r:
                out.update(r)
                out.update({'ok': True, 'kind': 'vendor'})
                out['symbol'] = currency_symbol(out['currency'])
                return out
            a = balance_auth if isinstance(balance_auth, dict) else {}
            if not (a.get('system_token') and a.get('user_id')):
                out['error'] = '需要平台凭据才能查余额'
                out['advice'] = f'{v['label']} 的余额记在账号上，要填「系统访问令牌 + 用户 ID」才查得到（{v.get('auth_hint', '')}）；模型用的 sk- 密钥查不到余额'
                return out
            if any((t['status'] in (401, 403) for t in tried)):
                out['error'] = '平台凭据无效或无权限'
                out['advice'] = '这个站拒绝了这对凭据（HTTP 401/403）：系统令牌重新生成过就会失效，用户 ID 也必须与它同属一个账号'
                return out
            out['error'] = f'没能从 {v['label']} 余额接口取到数值'
            out['advice'] = '响应结构可能已调整，可手动填写余额'
            return out
        try:
            r = _quota_vendor(v, base_url, api_key, protocol, extra_headers, timeout, tried)
        except Exception as e:
            r = None
            out['error'] = _short_err(e)
        if r:
            out.update(r)
            out.update({'ok': True, 'kind': 'vendor'})
            out['symbol'] = currency_symbol(out['currency'])
            return out
        sts = [t['status'] for t in tried]
        if not str(api_key or '').strip():
            out['error'] = '需要密钥才能查余额'
            out['advice'] = f'填上 {v['label']} 的 API Key 就能查余额'
            return out
        if any((s in (401, 403) for s in sts)):
            out['error'] = '密钥无效或无权限查询余额'
            out['advice'] = f'{v['label']} 的余额接口拒绝了这把密钥（HTTP 401/403）'
            return out
        out['error'] = f'没能从 {v['label']} 余额接口取到数值'
        out['advice'] = '响应结构可能已调整，可手动填写余额'
        return out
    if depth in ('relay', 'full'):
        try:
            r = _quota_relay(base_url, api_key, protocol, extra_headers, timeout, tried)
        except Exception as e:
            r = None
            out['error'] = _short_err(e)
        if r:
            if r.get('_auth'):
                out['error'] = '密钥无效或无权限查询余额'
                out['advice'] = '这个站是 one-api 系（账单端点存在），但拒绝了这把密钥'
                out['endpoint'] = r.get('endpoint', '')
                return out
            if r.get('_unlimited'):
                out['kind'] = 'relay'
                out['provider'] = 'one-api / new-api 中转'
                out['endpoint'] = r.get('endpoint', '')
                out['raw'] = r.get('raw', '')
                out['advice'] = '这是个「无限额度令牌」——额度记在账号上，只有浏览器登录态能看，API Key 读不到'
                return out
            if r.get('remaining') is not None or r.get('total') is not None:
                out.update({k: x for k, x in r.items() if not k.startswith('_')})
                out['ok'] = True
                out['kind'] = 'relay'
                out['symbol'] = currency_symbol(out.get('currency'))
                return out
        if depth == 'full':
            try:
                r = _quota_loose(base_url, api_key, protocol, extra_headers, timeout, tried)
            except Exception as e:
                r = None
            if r:
                out.update(r)
                out.update({'ok': True, 'kind': 'auto'})
                out['symbol'] = currency_symbol(out.get('currency'))
                out['advice'] = '自动识别到的字段（该站点没有在已知清单里），请核对是否真的是余额'
                return out
    if any((t['status'] in (401, 403) for t in tried)):
        out['error'] = '密钥无效或无权限'
        out['advice'] = '站点拒绝了这把密钥，或该接口不允许 API Key 访问'
        return out
    out['error'] = '该站点未提供可识别的余额接口'
    out['advice'] = '没找到公开的余额接口（有些站点只在余额不足时报错）。可以手动填写余额，或填一个自定义余额接口'
    return out

