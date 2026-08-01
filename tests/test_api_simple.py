"""
Simple API test script - runs against running server
"""
import requests

BASE_URL = "http://localhost:8000/api/v1"

def test_auth():
    print("\n=== Testing Auth API ===")
    
    resp = requests.post(f"{BASE_URL}/auth/login/", {
        "username": "admin",
        "password": "admin12345"
    })
    print(f"Login: {resp.status_code}")
    if resp.status_code == 200:
        data = resp.json()
        token = data["access"]
        print(f"Token obtained: {token[:20]}...")
        return token
    else:
        print(f"Login failed: {resp.text}")
        return None

def test_knowledge(token):
    print("\n=== Testing Knowledge API ===")
    
    headers = {"Authorization": f"Bearer {token}"}
    
    resp = requests.get(f"{BASE_URL}/knowledge/nodes/tree/", params={"root_type": "company_doc"}, headers=headers)
    print(f"Node tree: {resp.status_code}")
    if resp.status_code == 200:
        data = resp.json()
        print(f"Nodes found: {len(data)}")
    
    resp = requests.get(f"{BASE_URL}/knowledge/documents/", headers=headers)
    print(f"Documents: {resp.status_code}")

def test_analytics(token):
    print("\n=== Testing Analytics API ===")
    
    headers = {"Authorization": f"Bearer {token}"}
    
    resp = requests.get(f"{BASE_URL}/analytics/overview/", headers=headers)
    print(f"Overview: {resp.status_code}")
    
    resp = requests.get(f"{BASE_URL}/analytics/trend/", params={"days": 7}, headers=headers)
    print(f"Trend: {resp.status_code}")

def test_security(token):
    print("\n=== Testing Security API ===")
    
    headers = {"Authorization": f"Bearer {token}"}
    
    resp = requests.get(f"{BASE_URL}/security/ip-whitelist/", headers=headers)
    print(f"Whitelist: {resp.status_code}")
    
    resp = requests.get(f"{BASE_URL}/security/ip-blacklist/", headers=headers)
    print(f"Blacklist: {resp.status_code}")
    
    resp = requests.get(f"{BASE_URL}/security/login-attempts/", headers=headers)
    print(f"Login attempts: {resp.status_code}")

def test_audit(token):
    print("\n=== Testing Audit API ===")
    
    headers = {"Authorization": f"Bearer {token}"}
    
    resp = requests.get(f"{BASE_URL}/audit/logs/", headers=headers)
    print(f"Audit logs: {resp.status_code}")

if __name__ == "__main__":
    token = test_auth()
    if token:
        test_knowledge(token)
        test_chat(token)
        test_analytics(token)
        test_security(token)
        test_audit(token)
    print("\n=== Test completed ===")