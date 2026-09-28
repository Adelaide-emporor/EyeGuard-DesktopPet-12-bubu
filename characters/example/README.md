# 角色包模板（single 单图型，推荐）

1. 复制本文件夹并改名为角色英文名（如 `mycute`）。
2. `manifest.example.json` 重命名为 `manifest.json`，填入实际值。
3. `image` 指向一张透明背景 PNG（建议高度 400~900px，程序按 `scale` 缩放显示）。
   动画由程序对整图做位移（hop 跳跃）、左右摆动（sway_deg）、呼吸缩放（bob）
   实现，角色笔画不会变形。
4. `sounds` 可选：`stand`=起身提醒音、`sit`=坐下提醒音（wav），存在时优先播放。
5. 文件夹放到程序旁 `characters/`（或 `%APPDATA%\EyeGuard\characters\`），
   重启程序即可在“切换角色/桌宠形象”中选择。

帧序列型角色包（type="frames"）仍受支持：`states` 里每个动作列一组等尺寸
透明 PNG，`fps` 为播放速度。
