from js import Response, URL, Headers, fetch
import json
import base64
import ast
import hashlib
from urllib.parse import quote, unquote

# 全局缓存
_spider_config = None
_spider_code_cache = {}

# --------------------------
# 核心适配：自动给本地爬虫代码的requests.get加await（AST语法改写，无需修改原代码）
# --------------------------
class AwaitRequestsTransformer(ast.NodeTransformer):
    def visit_Call(self, node):
        if (isinstance(node.func, ast.Attribute) 
            and isinstance(node.func.value, ast.Name) 
            and node.func.value.id == 'requests' 
            and node.func.attr == 'get'):
            return ast.Await(value=node)
        return self.generic_visit(node)

def _transform_spider_code(code):
    tree = ast.parse(code)
    transformer = AwaitRequestsTransformer()
    transformed_tree = transformer.visit(tree)
    ast.fix_missing_locations(transformed_tree)
    return compile(transformed_tree, filename='<spider>', mode='exec')

# --------------------------
# 基础工具方法
# --------------------------
def _build_url(base, params=None):
    if not params:
        return base
    parts = []
    for k, v in params.items():
        if v is None:
            continue
        parts.append(f"{quote(str(k))}={quote(str(v))}")
    if not parts:
        return base
    sep = '&' if '?' in base else '?'
    return base + sep + '&'.join(parts)

async def _fetch(url, headers=None, params=None):
    full_url = _build_url(url, params)
    h = Headers.new()
    if headers:
        for k, v in headers.items():
            h.set(k, str(v))
    resp = await fetch(full_url, headers=h)
    class Resp:
        def __init__(self, resp, text):
            self.resp = resp
            self.status = resp.status
            self.text = text
            self.ok = 200 <= self.status < 300
        def json(self):
            return json.loads(self.text)
    text = await resp.text()
    return Resp(resp, text)

# 适配requests接口，完全兼容本地requests使用方式
class MockRequests:
    @staticmethod
    async def get(url, headers=None, params=None):
        return await _fetch(url, headers, params)
requests = MockRequests()

def _decode_ext(ext_str):
    if not ext_str:
        return {}
    try:
        pad = len(ext_str) % 4
        if pad:
            ext_str += '=' * (4 - pad)
        decoded = base64.b64decode(ext_str).decode('utf-8')
        return json.loads(decoded)
    except Exception:
        return {}

def _json(data, status=200):
    h = Headers.new()
    h.set("Content-Type", "application/json; charset=utf-8")
    h.set("Access-Control-Allow-Origin", "*")
    h.set("Access-Control-Allow-Methods", "GET, OPTIONS")
    body = json.dumps(data, indent=2, ensure_ascii=False)
    return Response.new(body, status=status, headers=h)

# --------------------------
# 爬虫加载逻辑
# --------------------------
async def _load_spider_config():
    global _spider_config
    if _spider_config:
        return _spider_config
    try:
        resp = await _fetch("https://000.hfr1107.top/live/py.json")
        if resp.ok:
            _spider_config = {item['name']: item['url'] for item in resp.json()}
    except Exception as e:
        _spider_config = {}
    return _spider_config

async def _load_spider_from_url(spider_url):
    url_hash = hashlib.md5(spider_url.encode()).hexdigest()
    if url_hash in _spider_code_cache:
        transformed_code = _spider_code_cache[url_hash]
    else:
        try:
            resp = await _fetch(spider_url)
            if not resp.ok:
                return None
            code = resp.text
            transformed_code = _transform_spider_code(code)
            _spider_code_cache[url_hash] = transformed_code
        except Exception:
            return None
    # 模拟运行环境
    local_vars = {}
    global_vars = globals().copy()
    global_vars['requests'] = requests
    # 兼容本地爬虫的base.spider导入
    class MockBaseSpider:
        pass
    global_vars['base'] = type('obj', (object,), {'spider': type('obj', (object,), {'Spider': MockBaseSpider})})()
    try:
        exec(transformed_code, global_vars, local_vars)
        Spider = local_vars.get('Spider')
        if not Spider:
            return None
        spider = Spider()
        if hasattr(spider, 'init'):
            spider.init("")
        return spider
    except Exception as e:
        print(f"加载爬虫失败[{spider_url}]: {str(e)}")
        return None

async def _load_spider_by_name(spider_name):
    config = await _load_spider_config()
    if spider_name not in config:
        return None
    return await _load_spider_from_url(config[spider_name])

# --------------------------
# 接口逻辑
# --------------------------
async def handle_debug():
    info = {
        "worker_alive": True,
        "available_spiders": list((await _load_spider_config()).keys()),
        "cached_spider_count": len(_spider_code_cache)
    }
    return _json(info)

async def handle_spider_request(spider, url):
    ac = url.searchParams.get("ac")
    t = url.searchParams.get("t")
    tid = url.searchParams.get("tid")
    pg = url.searchParams.get("pg")
    ext = url.searchParams.get("ext")
    extend = url.searchParams.get("extend")
    ids = url.searchParams.get("ids")
    flag = url.searchParams.get("flag")
    play = url.searchParams.get("play")
    wd = url.searchParams.get("wd")
    quick = url.searchParams.get("quick")
    f_val = url.searchParams.get("filter")

    try:
        if wd is not None and wd != "":
            result = await spider.searchContent(wd, quick or "", pg or "1")
            return _json(result)
        if play is not None:
            if hasattr(spider, 'playerContent'):
                if spider.playerContent.__code__.co_argcount == 4:
                    result = spider.playerContent(flag or "", play, [])
                else:
                    result = spider.playerContent(flag or "", play)
                return _json(result)
            return _json({"error": "当前爬虫不支持播放接口"}, 400)
        if ac == "detail" and ids:
            ids_list = [i.strip() for i in ids.split(",") if i.strip()]
            if not ids_list:
                return _json({"error": "ids参数为空"}, 400)
            result = await spider.detailContent(ids_list)
            return _json(result)
        cat_id = t or tid
        if cat_id:
            ext_str = ext or extend or ""
            extend_obj = _decode_ext(ext_str)
            result = await spider.categoryContent(cat_id, pg or "1", f_val or "", extend_obj)
            return _json(result)
        if ac == "homeVideo":
            result = await spider.homeVideoContent()
            return _json(result)
        result = spider.homeContent(True)
        try:
            hv = await spider.homeVideoContent()
            result['list'] = hv.get('list', [])
        except Exception:
            result['list'] = []
        return _json(result)
    except Exception as e:
        return _json({"error": f"接口处理失败: {str(e)}"}, 500)

# --------------------------
# 入口路由
# --------------------------
async def on_fetch(request, env):
    url = URL.new(request.url)
    if request.method == "OPTIONS":
        h = Headers.new()
        h.set("Access-Control-Allow-Origin", "*")
        h.set("Access-Control-Allow-Methods", "GET, OPTIONS")
        return Response.new("", status=204, headers=h)
    path = url.pathname
    if path == "/debug" or path.endswith("/debug"):
        return await handle_debug()
    
    spider = None
    path_parts = [p for p in path.split("/") if p]
    if not path_parts:
        return _json({
            "error": "请指定爬虫名称",
            "available_spiders": list((await _load_spider_config()).keys()),
            "usage": "访问格式: /爬虫名称?参数"
        })
    spider_name = path_parts[0]
    spider = await _load_spider_by_name(spider_name)
    if not spider:
        return _json({"error": "指定爬虫不存在"}, 404)
    
    return await handle_spider_request(spider, url)
