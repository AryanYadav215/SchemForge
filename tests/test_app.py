"""End-to-end API tests.  Run:  python -m unittest discover -s tests -v"""
import os
import re
import sys
import tempfile
import unittest

os.environ["SCHEMAFORGE_DATA"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as appmod  # noqa: E402


class Client:
    def __init__(self):
        self.c = appmod.app.test_client()
        self.token = None

    def _tok(self, path="/login"):
        html = self.c.get(path).get_data(as_text=True)
        self.token = re.search(r'name="csrf" content="([^"]+)"', html).group(1)

    def register(self, u, pw="secret1"):
        self._tok("/register")
        r = self.c.post("/register", data={"username": u, "password": pw, "csrf": self.token})
        assert r.status_code == 302, r.get_data(as_text=True)
        self._tok("/dashboard")

    def call(self, method, url, body=None):
        r = getattr(self.c, method.lower())(url, json=body, headers={"X-CSRF": self.token})
        return r.status_code, (r.get_json() if r.is_json else r.get_data(as_text=True))


def col(name, type_="VARCHAR", **kw):
    d = {"name": name, "type": type_}
    d.update(kw)
    return d


class AppFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.o = Client()
        cls.o.register("owner1")
        cls.m = Client()
        cls.m.register("member1")
        s, r = cls.o.call("POST", "/api/projects", {"name": "Library"})
        assert s == 200, r
        cls.pid = r["project_id"]
        cls.u = f"/api/p/{cls.pid}"

    def ok(self, method, path, body=None):
        s, r = self.o.call(method, self.u + path, body)
        self.assertEqual(s, 200, r)
        return r

    def bad(self, method, path, body=None, client=None):
        s, r = (client or self.o).call(method, self.u + path, body)
        self.assertEqual(s, 400, r)
        return r["error"]

    def test_01_design(self):
        self.ok("POST", "/tables", {"name": "author", "audit": True, "columns": [
            col("author_id", "INT", pk=True, auto=True), col("name", length=60, notnull=True)]})
        self.ok("POST", "/tables", {"name": "book", "columns": [
            col("book_id", "INT", pk=True, auto=True), col("title", length=100, notnull=True),
            col("price", "DECIMAL", precision=8, scale=2, check={"op": ">=", "value": "0"}),
            col("author_id", "INT", fk={"table": "author", "column": "author_id", "on_delete": "CASCADE"})]})
        self.ok("POST", "/tables", {"name": "member", "columns": [
            col("member_id", "INT", pk=True, auto=True), col("email", length=80, unique=True)]})
        r = self.ok("POST", "/relationships", {"kind": "M:N", "parent": "book", "child": "member", "junction": "loan"})
        self.assertTrue(any("loan" in s for s in r["sql"]))
        sch = self.ok("GET", "/schema")
        self.assertEqual({t["name"] for t in sch["tables"]}, {"author", "book", "member", "loan"})
        self.assertIn("table name", self.bad("POST", "/tables", {"name": "1bad", "columns": [col("a", "INT")]}))
        self.assertIn("references", self.bad("POST", "/tables", {"name": "x", "columns": [
            col("a", "INT", fk={"table": "nope", "column": "id"})]}))

    def test_02_data_and_constraints(self):
        self.ok("POST", "/rows/author", {"name": "Orwell"})
        self.ok("POST", "/rows/book", {"title": "1984", "price": "9.99", "author_id": "1"})
        self.assertIn("CHECK", self.bad("POST", "/rows/book", {"title": "x", "price": "-5"}))
        self.assertIn("Foreign key", self.bad("POST", "/rows/book", {"title": "x", "price": "1", "author_id": "99"}))
        self.assertIn("valid INT", self.bad("POST", "/rows/book", {"title": "x", "author_id": "abc"}))
        self.assertIn("too long", self.bad("POST", "/rows/author", {"name": "x" * 70}))
        self.ok("POST", "/rows/member", {"email": "a@b"})
        self.assertIn("Duplicate", self.bad("POST", "/rows/member", {"email": "a@b"}))
        rows = self.ok("GET", "/rows/book")
        self.assertEqual(rows["total"], 1)
        rid = rows["rows"][0]["_rid_"]
        self.ok("PUT", f"/rows/book/{rid}", {"title": "Nineteen Eighty-Four"})
        self.assertEqual(self.ok("GET", "/rows/book?q=Eighty")["total"], 1)
        audit = self.ok("GET", "/audit")["rows"]
        self.assertEqual([a["action"] for a in audit], ["INSERT"])

    def test_03_alter_keeps_data(self):
        self.ok("POST", "/tables/book/columns", {**col("isbn", length=13, unique=True), "notnull": False})
        self.assertIn("NOT NULL", self.bad("POST", "/tables/book/columns", {**col("pages", "INT"), "notnull": True}))
        self.ok("POST", "/tables/book/columns", {**col("pages", "INT"), "notnull": True, "default": "1"})
        self.ok("PUT", "/tables/book/columns/pages", col("page_count", "INT", notnull=True, default="1"))
        self.assertEqual(self.ok("GET", "/rows/book")["rows"][0]["page_count"], 1)
        self.ok("DELETE", "/tables/book/columns/isbn")
        self.ok("POST", "/tables/book/indexes", {"name": "idx_book_title", "columns": ["title"]})
        self.assertIn("foreign key", self.bad("DELETE", "/tables/author"))

    def test_04_query_builder(self):
        spec = {"table": "book", "alias": "b", "joins": [{"type": "INNER", "table": "author", "alias": "a", "left": "b.author_id", "right": "a.author_id"}],
                "columns": [{"col": "a.name"}, {"col": "*", "agg": "COUNT", "alias": "n"}], "group_by": ["a.name"],
                "having": [{"agg": "COUNT", "col": "*", "op": ">=", "value": "1"}]}
        r = self.ok("POST", "/query", spec)
        self.assertEqual(r["rows"], [["Orwell", 1]])
        sub = {"table": "author", "alias": "a", "columns": [{"col": "a.name"}],
               "where": [{"col": "a.author_id", "op": "IN_SUB", "sub": {"table": "book", "column": "author_id", "where": [{"col": "price", "op": ">", "value": "5"}]}}]}
        self.assertEqual(self.ok("POST", "/query", sub)["rows"], [["Orwell"]])
        self.assertIn("alias", self.bad("POST", "/query", {"table": "book", "columns": [{"col": "zzz.title"}]}))
        self.assertIn("no column", self.bad("POST", "/query", {"table": "book", "alias": "b", "columns": [{"col": "b.nope"}]}))

    def test_05_views_routines(self):
        self.ok("POST", "/views", {"name": "v_catalog", "sql": 'SELECT b.title, a.name FROM book b JOIN author a ON a.author_id=b.author_id'})
        self.assertEqual(self.ok("GET", "/view-rows/v_catalog")["rows"][0][1], "Orwell")
        self.ok("POST", "/routines", {"name": "rename_author", "body": "UPDATE author SET name = :n WHERE author_id = :id;\nSELECT name FROM author WHERE author_id = :id;"})
        r = self.ok("POST", "/routines/rename_author/call", {"params": {"n": "George Orwell", "id": "1"}})
        self.assertEqual(r["results"][-1]["rows"], [["George Orwell"]])
        r = self.ok("POST", "/sql", {"sql": "SELECT COUNT(*) AS n FROM book; SELECT 2 AS two"})
        self.assertEqual(len(r["results"]), 2)
        self.assertIn("Permission denied", self.bad("POST", "/sql", {"sql": "CREATE TABLE z(a)"}))

    def test_06_transactions(self):
        self.ok("POST", "/tx/begin")
        self.ok("POST", "/sql", {"sql": "INSERT INTO author(name) VALUES ('Temp1')"})
        self.ok("POST", "/tx/savepoint", {"name": "sp1"})
        self.ok("POST", "/sql", {"sql": "INSERT INTO author(name) VALUES ('Temp2')"})
        self.ok("POST", "/tx/rollback_to", {"name": "sp1"})
        names = [r[0] for r in self.ok("POST", "/sql", {"sql": "SELECT name FROM author ORDER BY author_id"})["results"][0]["rows"]]
        self.assertIn("Temp1", names)
        self.assertNotIn("Temp2", names)
        self.ok("POST", "/tx/rollback")
        names = [r["name"] for r in self.ok("GET", "/rows/author")["rows"]]
        self.assertNotIn("Temp1", names)
        self.assertIn("No open", self.bad("POST", "/tx/commit"))

    def test_07_privileges(self):
        s, _ = self.m.call("GET", self.u + "/schema")
        self.assertEqual(s, 404)                    # not a member yet
        self.ok("POST", "/members", {"username": "member1"})
        sch = self.m.call("GET", self.u + "/schema")[1]
        self.assertEqual(sch["tables"], [])         # no grants -> sees nothing
        s, r = self.m.call("GET", self.u + "/rows/author")
        self.assertEqual(s, 400)
        self.assertIn("Permission denied", r["error"])
        self.ok("POST", "/grant", {"username": "member1", "table": "author", "privilege": "SELECT"})
        self.assertEqual(self.m.call("GET", self.u + "/rows/author")[0], 200)
        s, r = self.m.call("POST", self.u + "/rows/author", {"name": "Nope"})
        self.assertEqual(s, 400)
        self.assertIn("Permission denied", r["error"])
        self.ok("POST", "/grant", {"username": "member1", "table": "author", "privilege": "INSERT"})
        s, r = self.m.call("POST", self.u + "/rows/author", {"name": "Member Added"})
        self.assertEqual(s, 200, r)
        s, r = self.m.call("POST", self.u + "/sql", {"sql": "SELECT * FROM book"})
        self.assertIn("Permission denied", r["error"])
        s, r = self.m.call("POST", self.u + "/sql", {"sql": "SELECT * FROM _audit_log"})
        self.assertEqual(s, 400)
        s, r = self.m.call("POST", self.u + "/sql", {"sql": "DROP TABLE author"})
        self.assertEqual(s, 400)
        s, r = self.m.call("POST", self.u + "/tables", {"name": "hack", "columns": [col("a", "INT")]})
        self.assertEqual(s, 400)
        self.assertIn("owner", r["error"])
        # audit trigger ran with the member's insert (definer rights)
        self.assertTrue(any(a["action"] == "INSERT" and "Member Added" in (a["new_data"] or "") for a in self.ok("GET", "/audit")["rows"]))
        # views: access through a view without access to the base tables
        self.ok("POST", "/grant", {"username": "member1", "table": "v_catalog", "privilege": "SELECT"})
        self.assertEqual(self.m.call("GET", self.u + "/view-rows/v_catalog")[0], 200)
        self.ok("POST", "/grant", {"username": "member1", "table": "author", "privilege": "ALL", "revoke": True})
        self.assertEqual(self.m.call("GET", self.u + "/rows/author")[0], 400)

    def test_08_normalize_export(self):
        self.ok("POST", "/tables", {"name": "enroll", "columns": [col("sid", "INT", pk=True), col("cid", "INT", pk=True), col("sname"), col("grade")]})
        self.ok("POST", "/fds/enroll", {"lhs": "sid", "rhs": "sname"})
        self.ok("POST", "/fds/enroll", {"lhs": "sid,cid", "rhs": "grade"})
        r = self.ok("GET", "/normalize/enroll")
        self.assertEqual(r["highest_normal_form"], "1NF")
        self.assertIn("CREATE TABLE", r["migration_3nf"])
        for d in ("sqlite", "postgres", "mysql", "oracle"):
            sql = self.ok("GET", f"/export?dialect={d}")["sql"]
            self.assertIn("CREATE TABLE", sql)
        self.assertIn("plpgsql", self.ok("GET", "/export?dialect=postgres")["sql"])
        self.assertEqual(self.o.c.get(self.u + "/dbfile").status_code, 200)

    def test_09_security(self):
        c2 = appmod.app.test_client()
        self.assertEqual(c2.post("/api/projects", json={"name": "q"}).status_code, 400)   # no csrf
        other = Client()
        other.register("stranger")
        self.assertEqual(other.call("GET", self.u + "/schema")[0], 404)
        self.assertEqual(other.call("GET", self.u + "/export?dialect=sqlite")[0], 404)
        self.assertEqual(self.o.call("GET", self.u + "/view-rows/x;drop")[0] in (400, 404), True)


if __name__ == "__main__":
    unittest.main()
