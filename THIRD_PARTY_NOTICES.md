# 第三方资源

根目录 MIT LICENSE 覆盖司南自身代码与文档。以下素材和依赖保留各自许可证。

## 城市照片

`travel-frontend-react/src/assets/img/CREDITS.md` 是照片来源、作者、许可的台账。
随仓照片来自 Wikimedia Commons，使用 CC BY 或 CC BY-SA；转换为 WebP、缩放与压缩后仍遵守原许可。
复用照片时请保留署名、来源与许可信息，修改后的 CC BY-SA 图片按其原许可共享。

## PDF 字体

`travel-agent-python/app/resources/fonts/SinanSansSC-Regular.ttf` 派生自 Noto Sans SC，使用 SIL OFL 1.1。
同目录 `OFL.txt` 为完整许可，`provenance.json` 记录上游提交、原始及产物 SHA256、转换参数。
转换后字体使用 Sinan Sans SC 名称，固定 400 字重，未额外裁剪上游字符集。不会在运行时下载字体。

## 软件依赖与地图

前端随仓的 Poppins 与 Geist 字体使用 SIL OFL，完整许可位于 `travel-frontend-react/public/fonts/`。

Python 与 JavaScript 依赖分别由 `uv.lock` 和 `package-lock.json` 锁定，保留包内许可证。
MapLibre、OpenFreeMap 与地图数据供应方的署名保留在地图控件内；在线底图并非离线资源。
项目不随仓分发地图瓦片。外部 API 的条款、配额和费用由自托管者确认。
