# AutoCAD / DXF Bridge MVP

本 MVP 是 KORYAO 的 CAD / DXF 无头生成 bridge 原型。它建立安全、可测试的 CAD plan 合约，并可在明确确认后真实生成和审计测试 DXF，而不是直接控制 AutoCAD。

## 为什么先做 DXF plan

直接控制 AutoCAD / DWG 会涉及本机授权、商业图纸、客户路径、COM 状态、窗口焦点和真实项目输出。DXF plan 能先把结构化几何、图层、单位和文本变成可验证数据，适合作为 Codex 后续生成 CAD 图形的中间层。

## 当前安全机制

- 默认 `dry_run=True`，不写 DXF 文件。
- 不调用真实 AutoCAD。
- 不打开 DWG。
- 不扫描用户目录。
- `dry_run=False` 时只允许写到 `examples/cad/output/`。
- 真实写入必须同时提供 `confirm_write=True`，且不会覆盖已有批次。
- 明确确认重试时，如果目标批次没有任何最终文件、只残留至少 15 分钟未更新的普通 `.staging` 文件，会先清理残留再重新生成；新鲜 staging、目录、符号链接或任一最终文件存在时仍拒绝覆盖。
- DXF 先写临时文件，再由 `ezdxf` 重新读取并执行 audit；回读的单位、图层、几何和文字会规范化为内容指纹，并与已批准 plan 的指纹比较。
- 只有实体数、内容指纹和 audit 全部通过，才从该回读文档生成 `<name>.preview.svg`。
- SVG 预览固定使用白色纸面和黑色前景，不继承模型空间的深色查看器默认值；交付前会验证该配色契约。
- 每条 SVG 路径都按回读顺序写入稳定实体 ID、DXF 图层和实体类型；verifier 会逐项对照回读 DXF，并记录映射指纹，路径数或任一元数据不一致时拒绝交付。
- DXF、无头 SVG 预览和 `<name>.manifest.json` 作为同一批次交付；任一产物失败就回滚当前批次。
- manifest 记录 DXF 与 SVG 的相对路径、字节数和 SHA-256，以及内容指纹、实体数、SVG 路径与图层计数、audit 结果、黑白配色和外部引用检查结果。
- manifest 写入 staging 后会重新解析，并逐项核对两个产物摘要、当前 staging 文件字节、DXF 内容指纹与 SVG 实体映射指纹；三项文件晋升到最终名称后还会整批回读一次，任一不一致都回滚。
- 真实生成会从 DXF 与 SVG 的规范化产物描述符计算稳定 `generation_id`，并同时写入 manifest、验证证据和调用结果，供调用方精确关联同一批次。
- manifest 与调用结果还会记录无路径、无时间戳的恢复证据：是否清理陈旧 staging、清理数量及最小年龄策略；staging 与最终交付 verifier 都会核对该证据。
- 输出统一经过 KORYAO sanitizer，不输出真实用户目录。

## 支持的实体类型

| 类型 | 必要字段 |
| --- | --- |
| `line` | `start`, `end`, `layer` |
| `polyline` | `points`, `layer` |
| `circle` | `center`, `radius`, `layer` |
| `rectangle` | `x`, `y`, `width`, `height`, `layer` |
| `text` | `position`, `value`, `height`, `layer` |

## 可选依赖

`ezdxf` 是可选依赖。没有安装时：

- `status()` 仍可返回 bridge 状态。
- `validate_cad_plan()` 和 `summarize_plan()` 仍可运行。
- `write_dxf(..., dry_run=True)` 仍可运行。
- `write_dxf(..., dry_run=False)` 会返回 warning 和 next_steps，不会崩溃。

安装 `ezdxf` 后，可以生成公开安全示例：

```powershell
python examples\cad\generate_dxf_plan.py `
  --confirm-write `
  --output starbridge_public_demo.dxf
```

成功条件不是“写文件调用没有报错”，而是 DXF、SVG 预览与 manifest 都存在；DXF 可重新读取、audit 错误数为 0、实体数与已校验 plan 一致，且回读单位、图层、几何和文字的内容指纹与 plan 一致；SVG 可解析、每条路径都能映射到一个回读实体并保留图层元数据，背景为 `#ffffff`、前景为 `#000000`，并且不含脚本、嵌入位图或外部引用。任一条件失败时只清理当前临时批次，不保留半成品。

## 后续扩展

1. 把 DXF plan 接入 KORYAO 核心 server，等待核心分支合并后再注册。
2. 增加更多实体：arc、dimension、mtext、hatch。
3. 增加 AutoCAD COM 打开 DXF 的可选验证，但默认关闭。
4. 研究 AutoCAD LT File IPC，不直接依赖窗口焦点。
5. 增加 DWG 打开验证前的人为确认流程。
