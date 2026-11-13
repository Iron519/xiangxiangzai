from langchain_core.tools import tool
from pydantic import BaseModel, Field
import requests
import urllib3
import os
import logging
from dotenv import load_dotenv, find_dotenv
load_dotenv(find_dotenv())
_TOOL_ERROR = os.getenv("FALLBACK_TOOL_ERROR", "相关查询暂时不可用，请稍后再试。").strip()
# 抑制SSL警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logger = logging.getLogger("ToolPersonalInfo")

class QueryPersonalInfo(BaseModel):
    empNo: str = Field(description="用户的工号")

def fetch_personal_info(input_parameters: QueryPersonalInfo):
    url = f"https://cnapi-service-i.ali.bba.cloud.bmw/hrappbestyou/hremployeeservice/v5/system/api/v1/employee/assistant/profile?empNo={input_parameters.empNo}"
    headers = {
        "apikey": "5984a9e3-4af4-46cb-bb00-4ce06ae8af90"
    }
    
    try:
        response = requests.get(url, headers=headers, verify=False, timeout=30)
        response.raise_for_status()
        response_json = response.json()
        
        if response_json.get("code") != 200:
            logger.error(f"API返回错误: {response_json}")
            return _TOOL_ERROR
        
        data = response_json["data"]
        
        result = {
            "婚姻状况": data.get("maritalStatus", []),
            "子女信息": data.get("childrenInfo", []),
            "最早工作日": data.get("firstWorkDay", ""),
            "学历信息": data.get("educationInfo", []),
            "邮寄地址": data.get("mailAddress", {}),
            "个人联系电话": data.get("phoneNumber", ""),
            "紧急联系人": data.get("emergencyContact", {})
        }
        return result

    except Exception as e:
        logger.error(f"查询个人信息失败: {e}")
        return _TOOL_ERROR

@tool
def query_personal_info(input_parameters: QueryPersonalInfo) -> dict:
    """
    根据员工工号，获取员工的个人资料信息，包括婚姻状况、子女信息、最早工作日、学历信息、邮寄地址、个人联系电话、紧急联系人。
    必填参数：员工工号
    """
    return fetch_personal_info(input_parameters)

# 测试
if __name__ == "__main__":
    parameters = QueryPersonalInfo(empNo="U0043154")
    result = query_personal_info(parameters)
    print(result)
