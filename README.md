# PDF 全书转换工具

使用 `pymupdf4llm` 将整本 PDF 转为 Markdown 或纯文本。默认对扫描页启用 OCR；`rapidocr-onnxruntime` 提供本地 OCR 引擎。转换在本机运行，并按页分块保存检查点。

## 使用方法（Windows PowerShell）

```powershell
.\.venv\Scripts\python.exe .\pdf_to_markdown.py "D:\资料\书名.pdf"
```

默认在 PDF 旁边生成同名 `.md` 文件。其他格式示例：

```powershell
# 输出纯文本
.\.venv\Scripts\python.exe .\pdf_to_markdown.py "D:\资料\书名.pdf" --format txt

# 同时输出 Markdown 和纯文本
.\.venv\Scripts\python.exe .\pdf_to_markdown.py "D:\资料\书名.pdf" --format both

# 指定输出文件；both 模式会生成 指定文件名.md 和 指定文件名.txt
.\.venv\Scripts\python.exe .\pdf_to_markdown.py "D:\资料\书名.pdf" --format both -o "D:\导出\书名"

# 如果书籍已经有文本层，可关闭 OCR 加快处理
.\.venv\Scripts\python.exe .\pdf_to_markdown.py "D:\资料\书名.pdf" --no-ocr

# 每 10 页保存一个检查点（默认每 20 页）
.\.venv\Scripts\python.exe .\pdf_to_markdown.py "D:\资料\书名.pdf" --chunk-pages 10

# 如果上次转换中断，重复相同命令会自动继续；如需放弃旧进度，从头开始
.\.venv\Scripts\python.exe .\pdf_to_markdown.py "D:\资料\书名.pdf" --restart
```

## 环境安装

项目依赖已安装到 `.venv`。重新创建环境时运行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Markdown 会尽量保留标题、阅读顺序和表格，并用分隔线标出页边界。纯文本会保留正文和表格内容，但不保留 Markdown 格式。扫描件会由 RapidOCR 自动识别；识别效果受扫描清晰度和倾斜影响。`--ocr-language` 是传给 OCR 引擎的语言提示，部分引擎可能忽略它。输出不会嵌入原 PDF 图片。

每个分块会先保存到输出旁边的隐藏工作目录（例如 `.书名.pdf_to_markdown.work`），其中包含 `state.json`、逐页片段和 `errors.jsonl`。分块或页面出错时，工具会继续处理后续页面；版面解析失败的页面会尝试降级为普通文本。`both` 模式会对每个分块只进行一次版面解析。全部页面完成后才会合并成最终文件；仍有无法提取的页面时不会生成新的最终文件。
