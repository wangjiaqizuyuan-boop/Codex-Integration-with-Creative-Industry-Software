# Vector60 本地基准

这里提供 Vector60 的本地 runner、匿名汇总器和未验证报告骨架。它们不会递归扫描目录，也不会把用户路径、输入文件名、素材内容、Token、Cookie 或账号状态写入汇总结果。

仓库未包含 40 张验收素材，因此当前 `report.md` 必须保持 `unverified`。没有真实数据和最终 SVG 原分辨率渲染证据时，不会生成通过结果。

## 显式 manifest

Runner 只读取 `vector60-manifest-v1` 中明确列出的 40 项：

- `logo_or_icon-01` 至 `logo_or_icon-10`
- `lineart-01` 至 `lineart-10`
- `flat-01` 至 `flat-10`
- `illustration-01` 至 `illustration-10`

manifest 根对象只允许 `schema_version` 和 `cases`。每个案例只允许 `case_id`、`category` 和 `input`；额外字段会使 manifest 整体失效。`input` 可以是相对于 manifest 的路径，但不会出现在 runner 的标准输出或报告中。

```json
{
  "schema_version": "vector60-manifest-v1",
  "cases": [
    {
      "case_id": "logo_or_icon-01",
      "category": "logo_or_icon",
      "input": "local-assets/item-01.png"
    }
  ]
}
```

上例省略了其余 39 项，因此只能用于说明 schema，不能运行。

Runner 不接受重复输入，不读取未列入 manifest 的文件，不允许输入位于输出目录之下，也不覆盖源图片。仓库内输出只能写入被 Git 忽略的 `examples/output/` 树；默认位置为 `examples/output/vectorization/vector60-benchmark/`。真实素材、SVG、渲染图和逐图输出不得提交。

## 安全运行

默认动作是 `dry-run`，只验证 schema、40 项分布、文件存在性和输出边界；不会打开图片、调用矢量化，也不会写文件。

```powershell
python -m benchmark.vector60.runner --manifest <本地-manifest.json>
python -m benchmark.vector60.runner --manifest <本地-manifest.json> --action validate
python -m benchmark.vector60.runner --manifest <本地-manifest.json> --action run
```

正式运行对每项先生成并评分 Artisan baseline，再运行 Artisan `auto-enhance`。增强阶段任意失败都回退到已验证的 `artisan_baseline.svg`。正式指标只来自最终 SVG 的原分辨率实际渲染；`preview.png`、缩放渲染和缺少安全验证的 SVG 都不能进入汇总。

Runner 生成匿名 case ID 对应的本地 SVG、最终渲染和 4 倍并排检查图。报告只记录匿名 case ID、指标、回退数和对比图复核状态，不记录输入或输出路径/文件名。自动生成的 4 倍图状态为 `generated_unreviewed`，只有人工复核后才能补充 `seam_free_4x` 证据。

## 匿名汇总

`aggregate.py` 只接受 `vector60-summary-v1` JSON。每个案例允许：

- `case_id`、`category`、`status`、可选 `fallback_used`
- SSIM、normalized MAE、Edge Dice
- 锚点、子路径、SVG bytes、评分耗时及对应 Artisan baseline
- `seam_free_4x`
- `safe_svg.no_bitmap`、`no_script`、`no_external_links`

根对象还可包含 Exact 三值验证与全量测试状态：

```json
{
  "schema_version": "vector60-summary-v1",
  "cases": [],
  "exact_validation": {
    "pixel_match": true,
    "different_pixel_count": 0,
    "maximum_channel_difference": 0
  },
  "test_suites": {
    "python": "passed",
    "frontend": "passed",
    "rust": "passed"
  }
}
```

上例仍缺少 40 个匿名案例，不能运行。

```powershell
python -m benchmark.vector60.aggregate --input <脱敏-summary.json> --format json
python -m benchmark.vector60.aggregate --input <脱敏-summary.json> --format markdown
```

## Vector60 硬门

- 40 张至少 38 张成功。
- Edge Dice 中位数不低于 0.90。
- normalized MAE 中位数不高于 0.08。
- 40 张均有 4 倍检查证据，至少 32 张无明显白缝。
- 锚点中位数比当前 Artisan baseline 减少至少 25%；或锚点中位数不增加超过 10%，且 Edge Dice 有显著配对提升。
- 40 张正式 SVG 均无位图、脚本和外链。
- Exact 保持 `pixel_match=true`、`different_pixel_count=0`、`maximum_channel_difference=0`。
- Python、前端和 Rust 全量测试均为 `passed`。

已知失败证据使对应硬门为 `failed`；缺少证据使其为 `unverified`。只有全部硬门通过时，总状态才是 `passed`。
