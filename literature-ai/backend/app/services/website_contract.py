"""Advertise the current website workflow and actual registered MCP route."""
import json
import socket

SCHEMA_VERSION = 'website_rebuild_prompt_v1'


def website_connection_contract(request, base_url):
    available = any(getattr(route, 'path', None) == '/mcp' for route in request.app.routes)
    prompt = (
        '在 Literature AI 中按文献库 → AI 整理图表 → 按反应模板填表 → 汇总分析流程工作。'
        '先读取指定 paper_id 的 PDF、图表和来源，再录入可核实的数据。'
        '每个数值保留单位及可回读来源；缺失值说明原因，冲突保留全部候选。'
        '读取接口不修改业务数据；执行真实 AI 提取或写入必须属于用户授权任务。'
    )
    config = {'mcpServers': {'literature-ai': {'url': base_url + '/mcp',
              'headers': {'Authorization':'Bearer litmcp_your_key'}}}} if available else {'mcpServers':{}}
    return {'base_url':base_url, 'hostname':socket.gethostname(), 'local_ip':request.url.hostname,
            'mcp_url':base_url+'/mcp' if available else None, 'mcp_available':available,
            'status':'available' if available else 'website_only',
            'status_message':'MCP 入口已注册' if available else '当前运行态未启用旧 MCP；请使用网站 AI 提取与数据表流程。',
            'prompt_schema_version':SCHEMA_VERSION,
            'prompt_contract':{'workflow':['library','figure_assets','data_table','summary_analysis'],
                               'requires_value_sources':True},
            'suggested_prompt':prompt, 'cursor_config':config, 'vscode_config':config,
            'cursor_config_json':json.dumps(config,ensure_ascii=False,indent=2),
            'vscode_config_json':json.dumps(config,ensure_ascii=False,indent=2)}
