"""An in-memory DigitalOcean API for the infra tests (Insights endpoints included) and a fake web for the
tentacles, the app, the Function and Spaces, both served through httpx.MockTransport.

The fake echoes secrets the way the real API does (database passwords, the Spaces secret key, the namespace key, a
kubeconfig token), so the no-secrets checks have something to find."""
from __future__ import annotations

import itertools
import json
import re
from typing import Any

import httpx

# Secrets the fake API hands out. The test environment adds the secret variables.
DB_PASSWORD = "AVNS_" + "plantedDbPassword01"
SPACES_SECRET = "planted-spaces-secret-01"
FN_KEY = "planted-functions-key-01"
KUBE_TOKEN = "planted-kube-token-01"
COLLECTIONS = {  # path under /v2: (list key, item key, id field)
    "projects": ("projects", "project", "id"), "vpcs": ("vpcs", "vpc", "id"),
    "firewalls": ("firewalls", "firewall", "id"), "reserved_ips": ("reserved_ips", "reserved_ip", "ip"),
    "databases": ("databases", "database", "id"), "functions/namespaces": ("namespaces", "namespace", "namespace"),
    "droplets": ("droplets", "droplet", "id"), "load_balancers": ("load_balancers", "load_balancer", "id"),
    "kubernetes/clusters": ("kubernetes_clusters", "kubernetes_cluster", "id"),
    "spaces/keys": ("keys", "key", "access_key"), "apps": ("apps", "app", "id"),
    "insights/notification-channels": ("notification_channels", "notification_channel", "id"),
    "insights/alert-rules": ("alert_rules", "alert_rule", "id"),
}
MUTATING = ("POST", "PUT", "PATCH", "DELETE")


def uuid(n: int) -> str:
    return f"00000000-0000-0000-0000-{n:012d}"


def reply(status: int, body: Any = None) -> httpx.Response:
    return httpx.Response(status) if body is None else httpx.Response(status, json=body)


def deep(obj: Any) -> Any:
    return json.loads(json.dumps(obj))


class FakeDO:
    """The DigitalOcean API as dicts. calls logs (method, path) of every API request, web_calls the rest."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.bodies: list[tuple[str, str, Any]] = []
        self.web_calls: list[tuple[str, str]] = []
        self.n = itertools.count(1)
        self.droplet_ips = (f"203.0.113.{i}" for i in itertools.count(10))  # .10 to .12, as in the head fixture
        self.reserved_ips = (f"198.51.100.{i}" for i in itertools.count(10))
        self.items: dict[str, dict[str, dict]] = {c: {} for c in COLLECTIONS}
        self.tags: set[str] = set()
        self.project_urns: dict[str, list[str]] = {}
        self.children: dict[str, set[str]] = {}
        self.deployments: dict[str, list[dict]] = {}
        self.registry: dict | None = {"name": "solutions-team", "region": "nyc3"}
        self.buckets: set[str] = set()
        self.reported = {"kraken-lb"}  # resource names Insights already has a resource_urn for
        self.fn_deployed = False
        self.fail: dict[tuple[str, str], int] = {}  # (method, path regex) -> status to answer instead
        self.sql_calls: list[tuple[dict, list[str]]] = []  # what the database step sent as doadmin
        self.sql_refuse: dict[str, str] = {}  # statement prefix -> the server's error for it
        self.items["vpcs"][uuid(900)] = {"id": uuid(900), "name": "default-tor1", "region": "tor1", "default": True}

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def sql(self, conninfo: dict, statements: list[str]) -> None:
        """The managed Postgres as steps.common.run_sql reaches it: records every session, refuses as told."""
        from steps.common import SqlError
        self.sql_calls.append((dict(conninfo), list(statements)))
        for statement in statements:
            refused = next((err for prefix, err in self.sql_refuse.items() if statement.startswith(prefix)), None)
            if refused:
                raise SqlError(refused)

    def web_transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle_web)

    def mutations(self) -> list[tuple[str, str]]:
        return [c for c in self.calls if c[0] in MUTATING]

    def count(self, method: str, path: str) -> int:
        return sum(1 for c in self.calls if c == (method, path))

    def body(self, method: str, path: str) -> list[Any]:
        return [b for m, p, b in self.bodies if (m, p) == (method, path)]

    # the API --------------------------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        body = json.loads(request.content) if request.content else None
        self.calls.append((method, path))
        self.bodies.append((method, path, body))
        for (m, pattern), status in self.fail.items():
            if m == method and re.fullmatch(pattern, path):
                return reply(status, {"id": "planted_failure", "message": "planted failure"})
        if path.endswith("/prom/api/v1/label/resource_urn/values"):
            name = re.search(r'resource_name="([^"]+)"', request.url.params.get("match[]", ""))
            return reply(200, {"status": "success", "data": self.urns(name.group(1) if name else "")})
        rest = path.removeprefix("/v2/")
        for coll in sorted(COLLECTIONS, key=len, reverse=True):
            if rest == coll or rest.startswith(coll + "/"):
                parts = [p for p in rest[len(coll):].split("/") if p]
                return self.collection(method, coll, parts, body or {}, request)
        return self.other(method, rest, body or {})

    def urns(self, name: str) -> list[str]:
        lb = next((o for o in self.items["load_balancers"].values() if o["name"] == name), None)
        return [f"do:loadbalancer:{lb['id']}"] if lb and name in self.reported else []

    def other(self, method: str, rest: str, body: dict) -> httpx.Response:
        if rest == "tags" and method == "POST":
            self.tags.add(body["name"])
            return reply(201, {"tag": {"name": body["name"]}})
        if rest.startswith("tags/"):
            name = rest.split("/", 1)[1]
            if name not in self.tags:
                return reply(404, {"id": "not_found", "message": "tag not found"})
            if method == "DELETE":
                self.tags.discard(name)
                return reply(204)
            return reply(200, {"tag": {"name": name}})
        if rest == "registry":
            if method == "POST":
                self.registry = {"name": body["name"], "region": "nyc3"}
                return reply(201, {"registry": self.registry})
            if self.registry is None:
                return reply(404, {"id": "not_found", "message": "no registry"})
            if method == "DELETE":
                self.registry = None
                return reply(204)
            return reply(200, {"registry": self.registry})
        if rest == "kubernetes/options":
            versions = [{"slug": "1.33.1-do.2", "kubernetes_version": "1.33.1"},
                        {"slug": "1.34.2-do.0", "kubernetes_version": "1.34.2"}]
            return reply(200, {"options": {"versions": versions}})
        if rest.startswith("actions/"):
            return reply(200, {"action": {"id": int(rest.split("/")[1]), "status": "completed"}})
        return reply(404, {"id": "not_found", "message": f"no fake for {method} /v2/{rest}"})

    def collection(self, method: str, coll: str, parts: list[str], body: dict,
                   request: httpx.Request) -> httpx.Response:
        list_key, item_key, id_field = COLLECTIONS[coll]
        store = self.items[coll]
        if not parts and method == "GET":
            tag = request.url.params.get("tag_name")
            found = [self.view(coll, o) for o in store.values() if not tag or tag in o.get("tags", [])]
            if coll == "insights/alert-rules":
                found = []  # finding A2: the list leaves rules out
            return reply(200, {list_key: found, "links": {}, "meta": {"total": len(found)},
                               "pagination": {"page": 1, "pages": 1, "per_page": 100}})
        if not parts and method == "POST":
            obj = self.make(coll, body)
            store[str(obj[id_field])] = obj
            answer = {item_key: self.view(coll, obj, created=True)}
            if coll == "droplets":
                answer["links"] = {"actions": [{"id": next(self.n), "rel": "create"}]}
            return reply(201 if coll != "droplets" else 202, answer)
        obj = store.get(parts[0])
        if obj is None:
            return reply(404, {"id": "not_found", "message": f"{item_key} not found"})
        if len(parts) > 1:
            return self.sub(method, coll, obj, parts[1:], body)
        if method == "GET":
            self.settle(coll, obj)
            return reply(200, {item_key: self.view(coll, obj)})
        if method == "PUT" and coll == "insights/alert-rules":
            assert "{{" not in json.dumps(body), "a placeholder was left in the rule"
            kept = obj["spec"].get("notification_channels")  # omitted on PUT keeps the bindings
            obj["spec"] = {**body["spec"], "notification_channels": body["spec"].get("notification_channels", kept)}
            obj["status"] = body.get("status") or obj["status"]  # omitted on PUT keeps the status
            return reply(200, {item_key: self.view(coll, obj)})
        if method == "PUT" and coll == "apps":
            stored = {e["key"]: e.get("value") for e in spec_envs(obj["spec"])}
            for env in spec_envs(body["spec"]):
                if str(env.get("value", "")).startswith("EV["):  # an encrypted value sent back unchanged
                    env["value"] = stored[env["key"]]
            obj["spec"] = body["spec"]
            self.deploy(obj)
            return reply(200, {"app": self.view(coll, obj)})
        if method == "DELETE":
            return self.remove(coll, obj, parts[0])
        return reply(405, {"message": "method not faked"})

    def make(self, coll: str, body: dict) -> dict:
        n = next(self.n)
        ident = uuid(n)
        if coll == "projects":
            self.project_urns[ident] = []
        if coll == "reserved_ips":
            return {"ip": next(self.reserved_ips), "region": {"slug": body["region"]}, "droplet": None}
        if coll == "droplets":  # the API never returns user_data, so the fake does not keep it on the Droplet
            kept = {k: v for k, v in body.items() if k != "user_data"}
            return {**kept, "id": 600000000 + n, "status": "new", "region": {"slug": body["region"]},
                    "networks": {"v4": [{"ip_address": next(self.droplet_ips), "type": "public"}]}}
        if coll == "databases":
            conn = {"host": "kraken-pg-0.db.example.test", "port": 25060, "user": "doadmin", "password": DB_PASSWORD,
                    "uri": f"postgresql://doadmin:{DB_PASSWORD}@kraken-pg-0.db.example.test:25060/defaultdb"}
            self.children[f"{ident}/users"] = {"doadmin"}
            return {**body, "id": ident, "status": "creating", "connection": conn,
                    "users": [{"name": "doadmin", "role": "primary", "password": DB_PASSWORD}]}
        if coll == "functions/namespaces":
            return {**body, "namespace": f"fn-{ident}", "uuid": ident, "key": FN_KEY,
                    "api_host": "https://faas-tor1-00000000.doserverless.co"}
        if coll == "spaces/keys":
            return {**body, "access_key": f"DO00FAKEACCESS{n:06d}", "secret_key": SPACES_SECRET}
        if coll == "apps":
            self.deployments[ident] = []
            app = {**body, "id": ident, "region": {"slug": "tor"},
                   "default_ingress": f"https://porthole-{n:05d}.ondigitalocean.app"}
            self.deploy(app)
            return app
        if coll == "insights/notification-channels":
            hook = body.get("webhook")
            safe = {k: v for k, v in (hook or {}).items() if k not in ("bearer_token", "signature", "basic_auth")}
            shape = {"webhook": {**safe, "bearer_token_status": {"configured": True}}} if hook else body
            return {**shape, "id": ident, "name": body["name"]}
        if coll == "insights/alert-rules":
            assert "{{" not in json.dumps(body), "a placeholder was left in the rule"
            return {"id": ident, "spec": body["spec"], "status": body.get("status"), "owner_id": 1}
        status = {"load_balancers": "new", "kubernetes/clusters": {"state": "provisioning"}}.get(coll)
        return {**body, "id": ident, **({"status": status} if status else {})}

    def settle(self, coll: str, obj: dict) -> None:
        """Resources become ready on the first read after creation, like a very fast cloud."""
        if coll == "droplets":
            obj["status"] = "active"
        elif coll == "load_balancers":
            obj.update(status="active", ip="203.0.113.20")
        elif coll == "databases":
            obj["status"] = "online"
        elif coll == "kubernetes/clusters":
            obj["status"] = {"state": "running"}

    def view(self, coll: str, obj: dict, created: bool = False) -> dict:
        out = deep(obj)
        if coll == "spaces/keys" and not created:
            out.pop("secret_key", None)
        if coll == "apps":
            for env in spec_envs(out["spec"]):
                if env.get("type") == "SECRET":
                    env["value"] = "EV[1:encrypted]"
            out["active_deployment"] = next((d for d in self.deployments[obj["id"]] if d["phase"] == "ACTIVE"), None)
        return out

    def deploy(self, app: dict) -> None:
        for d in self.deployments[app["id"]]:
            d["phase"] = "SUPERSEDED"
        self.deployments[app["id"]].insert(0, {"id": uuid(next(self.n)), "phase": "ACTIVE"})

    def sub(self, method: str, coll: str, obj: dict, parts: list[str], body: dict) -> httpx.Response:
        ident, what = str(obj.get("id") or obj.get("ip")), parts[0]
        if coll == "projects" and what == "resources":
            if method == "POST":
                self.project_urns[ident].extend(body["resources"])
            return reply(200, {"resources": [{"urn": u} for u in self.project_urns[ident]], "links": {}})
        if coll == "vpcs" and what == "members":
            return reply(200, {"members": [{"name": m["name"]} for m in self.members(ident)], "links": {}})
        if coll == "databases" and what in ("dbs", "users"):
            names = self.children.setdefault(f"{ident}/{what}", set())
            key = "db" if what == "dbs" else "user"
            item = {"name": body.get("name") or (parts[1] if len(parts) > 1 else "")}
            if what == "users":
                item["password"] = DB_PASSWORD
            if method == "POST":
                names.add(item["name"])
                return reply(201, {key: item})
            return reply(200, {key: item}) if item["name"] in names else reply(404, {"message": f"no such {key}"})
        if coll == "kubernetes/clusters" and what == "kubeconfig":
            kubeconfig = f"apiVersion: v1\nusers:\n- name: admin\n  user:\n    token: {KUBE_TOKEN}\n"
            return httpx.Response(200, text=kubeconfig)
        if coll == "apps" and what == "deployments":
            return reply(200, {"deployments": self.deployments[ident]})
        if coll == "reserved_ips" and what == "actions":
            droplet = self.items["droplets"].get(str(body.get("droplet_id")))
            obj["droplet"] = {"id": droplet["id"], "name": droplet["name"]} if droplet else None
            return reply(201, {"action": {"id": next(self.n), "status": "in-progress"}})
        if coll == "firewalls" and what == "rules":
            obj.setdefault("inbound_rules", []).extend(body.get("inbound_rules") or [])
            return reply(204)
        if coll == "load_balancers" and what == "droplets":
            obj["droplet_ids"] = sorted(set(obj.get("droplet_ids", [])) | set(body["droplet_ids"]))
            return reply(204)
        return reply(404, {"message": f"no fake for {method} {coll}/{ident}/{'/'.join(parts)}"})

    def members(self, vpc_id: str) -> list[dict]:
        return [d for c in ("droplets", "databases", "load_balancers", "kubernetes/clusters")
                for d in self.items[c].values() if vpc_id in (d.get("vpc_uuid"), d.get("private_network_uuid"))]

    def unassign(self, ident: str) -> None:
        for urns in self.project_urns.values():
            urns[:] = [u for u in urns if not u.endswith(f":{ident}")]

    def remove(self, coll: str, obj: dict, key: str) -> httpx.Response:
        ident = str(obj.get("id") or obj.get("ip"))
        if coll == "insights/notification-channels" and any(
                ident in json.dumps(r["spec"]) for r in self.items["insights/alert-rules"].values()):
            return reply(409, {"message": "the channel is used by alert rules"})
        if coll == "vpcs" and (obj.get("default") or self.members(ident)):
            return reply(403, {"message": "the VPC is a default VPC or has members"})
        if coll == "projects" and self.project_urns[ident]:
            return reply(412, {"message": "the project has resources"})
        del self.items[coll][key]
        self.unassign(ident)
        if coll == "droplets":
            for rip in self.items["reserved_ips"].values():
                if (rip.get("droplet") or {}).get("id") == obj["id"]:
                    rip["droplet"] = None
        return reply(200, {"id": ident}) if coll == "apps" else reply(204)

    # tentacles, the app, the function and Spaces ---------------------------------------------

    def handle_web(self, request: httpx.Request) -> httpx.Response:
        url, method = request.url, request.method
        self.web_calls.append((method, f"{url.scheme}://{url.host}{url.path}"))
        if url.port == 8800 and url.path == "/health":
            ips = {n["ip_address"] for d in self.items["droplets"].values() for n in d["networks"]["v4"]}
            return reply(200, {"name": "tentacle", "running": []}) if url.host in ips else reply(503, {})
        if url.host.endswith("doserverless.co"):
            return reply(200, {"pong": True}) if self.fn_deployed else reply(404, {"error": "not deployed"})
        if url.host.endswith("ondigitalocean.app") and self.items["apps"]:
            app = next(iter(self.items["apps"].values()))
            raw = next(e["value"] for e in spec_envs(app["spec"]) if e["key"] == "PORTHOLE_FLEET_JSON")
            tentacles = json.loads(raw).get("tentacles") or []
            if url.path == "/healthz":
                return reply(200, {"status": "ok", "version": "test", "fleet": {"tentacles": len(tentacles)}})
            return reply(200, {"tentacles": [{"name": t["name"], "reachable": True} for t in tentacles]})
        if url.host == "tor1.digitaloceanspaces.com":
            bucket = url.path.strip("/")
            if method == "HEAD":
                return httpx.Response(403 if bucket in self.buckets else 404)
            key = re.search(r"Credential=([^/]+)/", request.headers.get("authorization", ""))
            if not key or key.group(1) not in self.items["spaces/keys"]:
                return httpx.Response(403)
            if method == "PUT":
                self.buckets.add(bucket)
                return httpx.Response(200)
            self.buckets.discard(bucket)
            self.unassign(bucket)
            return httpx.Response(204)
        return httpx.Response(404)


def spec_envs(spec: dict) -> list[dict]:
    return [*spec.get("envs", []), *(e for s in spec.get("services", []) for e in s.get("envs", []))]
