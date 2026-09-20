PROMPT_VERSION = "paper-review-v2-2026.09.16.2"
PROMPT_TEMPLATE_ZH = """当前论文与明确关联 SI 已获得一次性论文图表审核写入授权。
先调用 get_paper_review_task 读取当前对象版本、任务指纹、图片、表格、PDF 定位、类型注册表和扁平 schema。
逐图逐表对照图片、caption、正文引用和原 PDF；图片标准类型不得只根据标题关键词猜测。
明确的参考文献列表、页眉、出版社标志、CrossMark 或版面碎片误识别直接使用 DELETE_FALSE_POSITIVE。
同一页裁图问题使用 RECROP；跨页的一个逻辑图片使用 COMPOSE_PAGES 并按 pages 顺序给出裁区。
每张科学图片填写独立标准类型、科学作用和自由格式中文详细解读；逐一核对真实子图。只要 panel_types 记录了子图，subfigures 就必须使用完全对应的逐个 label，并为每个 label 写入非空中文 description，说明对象或体系、坐标/颜色/曲线/结构或比较对象、分析方法、所得结论及其对整图和论文结论的作用。禁止只写 (a)、(b) 标签，禁止用 (a-b) 代替实际独立子图；真实没有独立子图时允许 panel_types=[] 且 subfigures=[]。缺少子图说明的复合图不得判为完整 RAG 解读。
提交前检查每个最终 image_path 对应文件真实存在且可完整解码；缺失或损坏图片必须 RECROP/COMPOSE_PAGES 修复，未修复时用 HOLD，不能报告 completed。证据不足的单项用 HOLD，但继续处理其他有效项目。
一篇论文最后只正式调用一次 apply_paper_review_batch，使用稳定 request_id、任务指纹和对象版本。
正常响应直接检查 authoritative_readback；只有响应超时或丢失时才调用 get_paper_review_receipt。
最终报告数据库权威回读，不得把 dry_run、HTTP 200 或工具调用成功当作全部写入成功。"""
def public_prompt() -> dict:
    return {"prompt_version": PROMPT_VERSION, "template_zh": PROMPT_TEMPLATE_ZH}
