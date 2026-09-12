# 配置目录

真正的运行时配置就在这个目录（与 `config/*.example.json` 并存），已被 `.gitignore` 忽略；
带 `.example` 的是**脱敏模板**，可以安全入库。

| 文件 | 说明 |
|---|---|
| `账户配置.json` | 真实账户配置：总资金、费率、风险偏好。首次使用可从模板复制，或跑 `python main.py aiplan init-account` 生成 |
| `模型配置.json` | 真实模型配置：providers（含 api_key）与 profiles。可跑 `python main.py aiplan init-models` 生成 |
| `账户配置.example.json` / `模型配置.example.json` | 脱敏模板：总资金为 0、api_key 为空，只留字段结构 |

个人数据（`持仓数据.md` / `自选股.md`）按「数据与配置分离」放在 `../data/user/`，同样被忽略。

**为什么放在这里？** `aiplan.py` 的默认路径常量直接指向本目录（`DEFAULT_ACCOUNT` /
`DEFAULT_MODELS`），所以 `python main.py aiplan post --code 002463.SZ` 不需要额外参数。
这三个 CLI 以自身所在目录定位文件，属于项目硬约束，改动会被 `scripts/check.py` 的哈希基线拦住。

写回：网页保存配置前会先留一份同名 `.bak`（覆盖式，不做无限堆积），改坏时改名覆盖即可。
