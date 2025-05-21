import json
import requests
from flask import request, session, redirect, url_for, Response

client_id = "hive.chatbot.console"
client_secret = "LnzCnfU9GR"


def get_aceess_code(url_prefix):
    """
    인증이 되었는지에 대한 확인
    return: access code key ex) 583ad4fc-1b98-4167-9c88-7f151c988840
    """
    
    response = f"https://test-oauth-manager.withhive.com/auth/oauth/authorize?client_id=hive.chatbot.console&response_type=code&redirect_uri=https://cads2-backoffice-test.withhive.com/mcp-agent/"
    
    return response


def get_access_token(access_code, url_prefix):
    print("access_code for get token:", access_code)
    data = {
        'grant_type': 'authorization_code',
        'code': str(access_code),
        'redirect_uri': ''
    }

    data['redirect_uri'] = f"https://cads2-backoffice-test.withhive.com/mcp-agent/"
    response = requests.post(
        'https://test-oauth-manager.withhive.com/auth/oauth/token', data=data, auth=('hive.chatbot.console', 'LnzCnfU9GR'))
    
    print("*********get token***************")
    print(data)
    print(response)
    print(response.status_code)
    print(response.text)
    print("************************************")
    res_code = response.status_code
    if res_code != 200:
        print("res_code:", res_code)
        print("not status 200")
        return None
    res_text = response.text
    res_data = json.loads(res_text)
    return res_data


def get_user_info(access_token):
    response = requests.get(
        "https://test-oauth-manager.withhive.com/api/user/get-user", headers={"Authorization": f"bearer {access_token}"})
    

    print("************get name**************")
    res_code = response.status_code
    res_text = response.text
    res_data = json.loads(res_text)
    print(res_code)
    print(res_text)
    print(res_data)
    print("***********************************")
    return res_data


def get_token_with_code(url_prefix):
    """
    인증이 되었는지에 대한 확인
    return: access code key ex) 583ad4fc-1b98-4167-9c88-7f151c988840
    """
    
    response = f"https://test-oauth-manager.withhive.com/auth/oauth/authorize?client_id=hive.chatbot.console&response_type=code&redirect_uri=https://dev-chatbotcs-training.withhive.com/{url_prefix}/"

    print(response.content)
    print("response.url:", response.url)
    print("********* get code **********")
    print(response)
    print("*****************************")

    return response

def insert_log_info(log_dict):
    # tb_backboard_log에 정보 insert
    #        메뉴     | 관리자ID |       수정 날짜      |  작업 내용 |    수정 대상        | 이력
    #      대시보드   | daewook  | 2021-07-06 15:36:37  |    조회    | 서머너즈워: 백년전쟁 | 조회
    # 데이터 수집 관리 | daewook  | 2021-07-06 15:36:37  |    요청    | 서머너즈워: 백년전쟁 | 조회
    pass

def cookie_check(request_page):
    print(request_page)
    print(type(request_page))
    print("~ Check Cookies Start~")
    print(request_page.cookies)
    print("~ Check Cookies Finished ~")

    if request_page.cookies.get('access_token') is not None:
        print("Token is in cookie.")
        # 있을 때 유효성 검사
        access_token = request_page.cookies.get('access_token')
        if is_valid_token(access_token):
            return True
        elif not is_valid_token(access_token):
            return False
    else:
        print("Token is not in cookie.")
        return False

def is_valid_token(access_token):
    # check token params
    params = {'token': access_token}

    response = requests.get(
        "https://test-oauth-manager.withhive.com/auth/oauth/check_token", params=params)

    # print("************get name**************")
    res_code = response.status_code
    res_text = response.text
    res_data = json.loads(res_text)
    res_code = res_data['result']['code']
    print("Token Status:", res_code)
    # 정상
    if res_code == 100:
        return True
    # 토근 만료
    elif res_code == 501:
        return False
    # 토큰 미입력
    elif res_code == 502:
        return False
    else:
        return False

def get_user_id():
    return request.cookies.get('user_id')

def get_lang(request):
    lang = 'ko'
    if request.args.get("lang") is not None:
        lang = request.args.get("lang")
    else:
        if request.cookies.get("lang") is not None:
            lang = request.cookies.get("lang")

    return lang


if __name__ == '__main__':
    get_aceess_code()
    # get_kname("08cb1359-d1ff-4880-9340-af28801ffa55")
