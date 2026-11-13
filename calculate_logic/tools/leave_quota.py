from langchain_core.tools import tool
from pydantic import BaseModel, Field
from typing import Any, Callable, Optional, Sequence, Literal
import requests
import urllib3
import os
from dotenv import load_dotenv, find_dotenv
load_dotenv(find_dotenv())
_TOOL_ERROR = os.getenv("FALLBACK_TOOL_ERROR", "相关查询暂时不可用，请稍后再试。").strip()
# 抑制SSL警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class QueryLeaveQuota(BaseModel):
    empNo: str = Field(description="用户的工号")
    startDate: Optional[str] = Field(default='01/01/2026', description="查询区间的起始日期，格式为dd/mm/yyyy")
    endDate: Optional[str] = Field(default='31/12/2026', description="查询区间的结束日期，格式为dd/mm/yyyy")
    leaveType: Literal["带薪年休假", "全薪病假", "半薪病假", "无薪休假"] = Field(default="带薪年休假", description="假期类型")

def fetch_leave_quotas(input_parameters: QueryLeaveQuota):
    url = "https://cnapi-service-i.ali.bba.cloud.bmw/hrappbestyou/hremployeeservice/v4/time/api/v1/time/assistant/leaveQuotas"
    headers = {
        "Content-Type": "application/json",
        "Accept-Language": "zh-CN",
        "apikey": "5984a9e3-4af4-46cb-bb00-4ce06ae8af90"
    }
    
    body = {
        "startDate": input_parameters.startDate,
        "endDate": input_parameters.endDate,
        "empNo": input_parameters.empNo
    }
    
    try:
        response = requests.post(url, json=body, headers=headers, verify=False, timeout=30)
        response.raise_for_status()  # 检查HTTP错误
        response_json = response.json()
        quotas = response_json["data"]["quotas"]
        result = {}
        for quota in quotas:
            if input_parameters.leaveType == quota["ktext"]:
                total = quota["anzhl"]
                used = quota["kverb"]
                result = {
                    "总额": total,
                    "已用": used,
                    "剩余": total - used
                }
        return result

    except requests.exceptions.RequestException as e:
        print(f"请求失败: {e}")
        return _TOOL_ERROR

@tool
def query_leave_quota(input_parameters: QueryLeaveQuota) -> dict:
    """
    根据员工工号，获取员工各项假期的配额、用量和余额，包括“带薪年休假”、“全薪病假”、“半薪病假”、“无薪休假”...
    必填参数：员工工号、休假类型
    """
    return fetch_leave_quotas(input_parameters)

# 测试
if __name__ == "__main__":
    parameters = QueryLeaveQuota(empNo="U0048399", leaveType="全薪病假")
    result = query_leave_quota(parameters)
    print(result)




