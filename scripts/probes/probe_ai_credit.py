import asyncio, sys
sys.path.insert(0, "src")
from aiforge.config import store
from aiforge.llm import DeepSeekClient, LlmError

async def main():
    cfg = store()
    api = cfg.config.api
    client = DeepSeekClient(cfg.api_key, api.base_url, api.model, api.timeout_s)
    try:
        spec = await client.parse_design_spec("做一个 30x30x30mm 的方块")
        print("调用成功，规格条目：", len(spec.specs))
    except LlmError as exc:
        print("LlmError:", exc.message)
        print("hint:", exc.hint)
        print("code:", exc.code)
    except Exception as exc:
        print("异常：", type(exc).__name__, exc)

asyncio.run(main())
