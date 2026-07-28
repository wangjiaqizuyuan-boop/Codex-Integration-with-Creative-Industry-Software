# 4. Codex 接入 Blender

这份文档说明 Blender 桥的真实状态。当前仓库有 Blender 接入说明、`bridge.json` manifest、环境探针、公开安全 `scene_plan` 和参考图重建 dry-run 计划，状态是 `prototype`。真实渲染闭环和 Blender MCP 示例还没有完成。

公开仓库只保存通用说明、状态检查和后续脚本入口，不保存私有 `.blend`、贴图、资产库、渲染缓存或本机路径。

## 当前可运行

| 能力 | 入口 | 说明 |
| --- | --- | --- |
| manifest | `examples/blender_bridge/bridge.json` | 声明状态、入口、支持任务和安全说明 |
| 环境探针 | `examples/blender_bridge/probe.py` | 检查 Blender 可执行文件、本机配置和公开安全 report |
| 安全场景计划 | `examples/blender_bridge/scene_plan.py` | 生成固定模板 dry-run 场景计划，不启动 Blender，不打开 `.blend` |
| 固定场景生成 | `examples/blender_bridge/render_fixed_scene.py` | 默认 dry-run；显式确认后在 Blender 后台生成公开基础场景、预览图和回执 |
| 参考图重建计划 | `examples/blender_bridge/reference_reconstruction_plan.py` | 生成防幻觉重建 dry-run 计划：先分割/估深/量测，再做同相机渲染误差校验 |
| 总状态探测 | `examples/bridge_status.py` | 检查 `BLENDER_EXE`、常见安装路径和 `BLENDER_MCP_DIR` |

`blender.scene_plan` 同时返回版本化 `artifact_contract`。未来确认后的本地执行器只有在同一批次得到以下三个产物，并为每项记录相对路径、字节数和 SHA-256 时，才能声明生成完成：

- `scene.blend`：可继续编辑的 Blender 场景。
- `preview.png`：固定相机的预览渲染。
- `receipt.json`：记录计划版本和执行结果的脱敏回执。

三项采用原子完成规则：缺少任一产物、哈希缺失或格式不符都必须把整批标记为 `failed`，清理仅限当前批次，不能把部分结果冒充成功。当前实现仍只返回 dry-run 契约，不创建这些文件。

## 固定场景生成闭环

先预览计划，不会启动 Blender 或写文件：

```powershell
python examples\blender_bridge\render_fixed_scene.py --json
```

确认后才执行固定模板，输出目录必须位于仓库已忽略的 `output/` 下，且目标批次不能已经存在：

```powershell
python examples\blender_bridge\render_fixed_scene.py `
  --confirm-run `
  --output-dir output/blender/fixed-scene-001 `
  --json
```

执行器只向 Blender 传入仓库内已审计脚本，不接受用户 Python、`.blend` 输入、贴图或外部资产。它先写同级临时批次；仅当 `scene.blend`、`preview.png` 和 `receipt.json` 齐全且可验证时才原子移动到最终目录。失败只清理当前临时批次，保留既有成功结果。

如果没有检测到 Blender，命令返回 `blender_not_detected` 结构化错误，不创建目录。本仓库的 CI 只验证 dry-run、路径限制、确认门和失败恢复，不把未安装 Blender 的环境写成真实渲染成功。

## 需要本机安装什么

- Blender desktop 或 blender CLI。
- Python 3.10+。
- 如果使用 MCP 方向，需要本机 Blender MCP 项目目录。

真实路径只放本机环境变量：

```powershell
$env:BLENDER_EXE="<path-to-blender.exe>"
$env:BLENDER_MCP_DIR="<path-to-blender-mcp>"
```

## 验证命令

```powershell
npm.cmd run status:probe:json
```

直接运行：

```powershell
python examples\blender_bridge\probe.py
python examples\bridge_status.py --probe-executables --json
python examples\blender_bridge\scene_plan.py --json
python examples\blender_bridge\render_fixed_scene.py --json
python examples\blender_bridge\reference_reconstruction_plan.py --json
npm.cmd run blender:reference:plan
```

状态脚本会优先读取 `BLENDER_EXE` 和 `BLENDER_MCP_DIR`，也会检查常见安装路径。不要把个人路径写进公开文档。

## 参考图验证重建路线

`blender.reference_reconstruction_plan` 先把用户图片建模拆成可审计阶段，不读取图片、不启动 Blender，也不声明已经能自动恢复真实 3D。它要求后续真实本地流程必须有：

- Photoshop / SAM2 / GroundingDINO 这类分割结果：轮廓、部件、材质区域。
- VGGT / Depth Anything 3 / MapAnything 这类几何初始化：相机、深度、点云或粗网格。
- 单视图量测：消失点、透视线、已知尺度锚点。
- Blender 语义场景图：相机、受约束网格、材质槽和 modifier stack。
- 同相机渲染反查：轮廓 IoU、边缘像素误差、可见部件覆盖率、材质区域 IoU。

如果只有单张图且没有尺度锚点，输出只能声明为 `reference_view_match_only`，不能把背面、遮挡内部、真实厚度或绝对尺寸说成事实。交付前必须产出同相机差异报告和 evidence manifest；误差未过阈值时状态应保持 `needs_user` 或继续迭代。

完整的 GitHub 项目分组、误差指标和接入顺序见 [Blender reference-verified reconstruction](blender-reference-verified-reconstruction.md)。

## 不能做什么

- 只有显式 `--confirm-run` 的固定公开场景脚本会启动 Blender；没有本机成功回执时不能声称已经生成或渲染。
- 不能提交私有 `.blend`、贴图、HDRI、资产库、渲染缓存。
- 不能提交商业模型、购买素材或客户场景。
- 不能让 CI 依赖真实安装 Blender。

## 下一步

1. 保持 `scene_plan` 只生成固定模板计划，不开放任意 Python。
2. 保持 `reference_reconstruction_plan` 只生成参考图验证计划，不读取用户图片或启动 Blender。
3. 增加确认后的 `render_probe.py`，验证 Blender 可执行文件、渲染器和输出目录。
4. 只把渲染输出放在本机 `output/` 或临时目录，不提交图片。
5. 评估 Blender MCP、BlenderProc、VGGT、Depth Anything 3、SAM2、GroundingDINO 和 PyTorch3D 等项目结构，再决定是否纳入本仓库。
