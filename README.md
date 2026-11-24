# KeyShot 工程瘦身

给 KeyShot 工程清理内容完全相同的重复工作室预览图，生成一个更小的工程副本。支持批量添加 BIP 和 KSP，处理在本机完成，源文件会保留。

## 下载和使用

1. 去 [GitHub Releases](https://github.com/panghuhuhu123/keyshot-project-slimmer/releases/latest)，下载 Windows x64 ZIP 并解压。
2. 双击 `KeyShot 工程瘦身 1.9.exe`，把 `.bip`、`.ksp` 或包含工程的文件夹拖入窗口。
3. 点「分析文件」查看预计可释放空间，再点「生成瘦身副本」。默认将副本保存到源文件旁，文件名带 `_瘦身`。

操作细节见 [使用说明](使用说明.md)。

## 适用范围

已验证 KeyShot Studio 2025.1 的压缩和未压缩 BIP，以及包含一份此类 BIP 的 KSP 工程包。工具保留正在使用的预览、模型网格、NURBS 曲面、细分参数及 KSP 内的贴图等资源。重要工程可以用 KeyShot 分别打开原文件和副本核对。

## 从源码运行

在 Windows x64 上用 Python 3.12 运行以下命令。

```powershell
python -m pip install -r src/requirements.txt
python src/app.py
```

## 使用许可

**个人和公司均可免费使用，包括内部工作和收费客户项目。** 欢迎分享官方下载页，也可免费转发完整的官方发布包。转售、收费下载或随付费产品分发须事先取得书面授权，请联系 `2438843039@qq.com`。详见 [软件许可](LICENSE) 和 [第三方许可](THIRD-PARTY-NOTICES.md)。

本项目由第三方独立开发。KeyShot 是其权利人的商标。
