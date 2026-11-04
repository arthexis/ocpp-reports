from fastapi import FastAPI
from fastapi.testclient import TestClient
from ocpp_reports.dashboard_login import attach_login, provision

def test_login_http(tmp_path):
    path=str(tmp_path/"auth.sqlite")
    provision(path,"tester","long-password-for-testing","operator","tenant-a")
    app=FastAPI()
    @app.get("/")
    def index(): return {"ok":True}
    @app.get("/api/event-group")
    def events(): return {"ok":True}
    attach_login(app,path=path,tenant="tenant-a")
    with TestClient(app,base_url="https://reports.example.test") as client:
        assert client.get("/").status_code==403
        assert client.get("/login").status_code==200
        response=client.post("/login",data={"username":"tester","password":"long-password-for-testing"},headers={"origin":"https://reports.example.test"},follow_redirects=False)
        assert response.status_code==303
        assert client.get("/").status_code==200
        assert client.get("/api/event-group").status_code==403
