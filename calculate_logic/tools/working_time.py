from langchain_core.tools import tool
from pydantic import BaseModel, Field
from typing import Optional
import requests
import urllib3
import logging
# 抑制SSL警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logger = logging.getLogger("ToolWorkingTime")

class QueryWorkingTime(BaseModel):
    empNo: str = Field(description="用户的工号")
    yearmonth: str = Field(description="查询的年月，格式为YYYYMM，例如202607，默认为当月")

def fetch_working_time(input_parameters: QueryWorkingTime):
    url = "https://cnapi-service-i.ali.bba.cloud.bmw/hrappbestyou/hremployeeservice/v4/time/api/v1/time/assistant/wta"
    headers = {
        "Content-Type": "application/json",
        "Accept-Language": "zh-CN",
        "apikey": "5984a9e3-4af4-46cb-bb00-4ce06ae8af90"
    }
    
    body = {
        "yearmonth": input_parameters.yearmonth,
        "empNo": input_parameters.empNo
    }
    
    try:
        response = requests.post(url, json=body, headers=headers, verify=False, timeout=30)
        response.raise_for_status()
        response_json = response.json()
        
        if response_json.get("code") != 200:
            logger.error(f"API返回错误: {response_json}")
            return "查询工时暂时不可用，请稍后再试"
        
        data = response_json["data"]
        es_wta = data.get("esWTA", {})
        et_detail = data.get("etDETAIL", {}).get("item", [])
        et_wtaday = data.get("etWTADAY", {}).get("item", [])
        et_workschedule = data.get("etWORKSCHEDULE", {}).get("item")
        
        # 当月数据
        atendm = es_wta.get("atendm", 0)  # 当期出勤小时
        standm = es_wta.get("standm", 0)  # 当期标准小时
        
        # 计算补偿工时
        zwta_sum = 0
        for day in et_wtaday:
            zwta_sum += day.get("zwta", 0)
        
        current_month_hours = round(atendm - standm + zwta_sum, 2)
        
        # 年度/累计数据
        balan = es_wta.get("balan", 0)  # 工时账户总余额
        deviacum = es_wta.get("deviacum", 0)  # 累计差异小时
        leavecum = es_wta.get("leavecum", 0)  # 累计补偿小时
        wtalastyear = es_wta.get("wtalastyear", 0)  # 上年小时
        paywta = es_wta.get("paywta", 0)  # 月度已结算工时账户
        netwta = es_wta.get("netwta", 0)  # 年度储备工时账户
        wtazbl1 = es_wta.get("wtazbl1", 0)  # 2025年前上一年账户
        wtazbc1 = es_wta.get("wtazbc1", 0)  # 2025年前当年账户
        wtazbl3 = es_wta.get("wtazbl3", 0)  # 累计已平衡
        wtazlb2 = es_wta.get("wtazlb2", 0)  # 2025年后1月相关
        
        result = {
            "当月出勤小时": atendm,
            "当月标准小时": standm,
            "当月补偿工时": zwta_sum,
            "当月净工时": current_month_hours,
            "工时账户总余额": balan,
            "累计差异小时": deviacum,
            "累计补偿小时": leavecum,
            "上年工时": wtalastyear,
            "已结算工时": paywta,
            "储备工时账户": netwta,
            "2025年前上一年账户": wtazbl1,
            "2025年前当年账户": wtazbc1,
            "累计已平衡": wtazbl3,
            "2025年后1月账户": wtazlb2
        }
        return result

    except Exception as e:
        logger.error(f"查询工时失败: {e}")
        return "查询工时暂时不可用，请稍后再试"

@tool
def query_working_time(input_parameters: QueryWorkingTime) -> dict:
    """
    根据员工工号和年月，获取员工的生产工时/WTA数据，包括当月出勤、标准、补偿、净工时，以及年度累计数据如总余额、前期账户、已结算、储备账户等。
    必填参数：员工工号、年月(YYYYMM格式)
    """
    return fetch_working_time(input_parameters)

# 测试
if __name__ == "__main__":
    parameters = QueryWorkingTime(empNo="U0043154", yearmonth="202607")
    result = query_working_time(parameters)
    print(result)
