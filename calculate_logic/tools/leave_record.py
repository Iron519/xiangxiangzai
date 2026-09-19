from langchain_core.tools import tool
from pydantic import BaseModel, Field
from typing import Any, Callable, Optional, Sequence, Literal
import requests
import urllib3
# 抑制SSL警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class QueryLeaveRecord(BaseModel):
    empNo: str = Field(description="用户的工号")
    startDate: Optional[str] = Field(default='01/01/2026', description="查询区间的起始日期，格式为dd/mm/yyyy")
    endDate: Optional[str] = Field(default='31/12/2026', description="查询区间的结束日期，格式为dd/mm/yyyy")
    leaveType: Literal["带薪年休假", "全薪病假", "半薪病假", "半薪病假无配额限制", "无薪休假"] = Field(default="半薪病假", description="假期类型")

def fetch_leave_records(input_parameters: QueryLeaveRecord):
    url = "https://cnapi-service-i.ali.bba.cloud.bmw/hrappbestyou/hremployeeservice/v4/time/api/v1/time/assistant/leaveAbsence"
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
    
    leave_records = []
    try:
        response = requests.post(url, json=body, headers=headers, verify=False, timeout=30)
        response.raise_for_status()  # 检查HTTP错误
        response_json = response.json()
        records = response_json["data"]["leaveAbsenceList"]
        # print(f"查询结果：{records}")
        result = {}
        for record in records:
            if input_parameters.leaveType == record["atext"]:
                type = record["atext"]
                duration = record["abwtg"]
                begda = record["begda"]
                endda = record["endda"]
                result = {
                    "假种": type,
                    "天数": duration,
                    "起始日期": begda,
                    "结束日期": endda
                }
                leave_records.append(result)
        return leave_records

    except requests.exceptions.RequestException as e:
        print(f"请求失败: {e}")
        return None

@tool
def query_leave_record(input_parameters: QueryLeaveRecord) -> dict:
    """
    根据员工工号，获取员工各项假期的休假记录，包括以下信息：假种、休假天数、起始日期、结束日期
    必填参数：员工工号、休假类型
    """
    return fetch_leave_records(input_parameters)

# 测试
if __name__ == "__main__":
    parameters = QueryLeaveRecord(empNo="U0048399", leaveType="带薪年休假")
    result = query_leave_record(parameters)
    print(result)
