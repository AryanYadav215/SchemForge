"""Build the demo database used in the report and the viva demo.

    python seed_demo.py

Creates two users (password for both: demo1234)
    demo          - owner of the "Library" database (tables, data, views, routines, grants)
    amit_viewer   - member who can only SELECT book / v_catalog and INSERT into loan
then start the app with  python app.py  and sign in as demo.
Safe to run twice: it stops if the demo user already exists.
"""
import re
import sys

import app as appmod


class C:
    def __init__(self):
        self.c = appmod.app.test_client()
        self.token = None

    def tok(self, path):
        html = self.c.get(path).get_data(as_text=True)
        self.token = re.search(r'name="csrf" content="([^"]+)"', html).group(1)

    def register(self, user, pw="demo1234"):
        self.tok("/register")
        r = self.c.post("/register", data={"username": user, "password": pw, "csrf": self.token})
        if r.status_code != 302:
            return False
        self.tok("/dashboard")
        return True

    def call(self, method, url, body=None):
        r = getattr(self.c, method.lower())(url, json=body or {}, headers={"X-CSRF": self.token})
        if r.status_code >= 400:
            raise SystemExit(f"{method} {url} failed: {r.get_json() if r.is_json else r.status_code}")
        return r.get_json()


def col(name, t="VARCHAR", **k):
    d = {"name": name, "type": t}
    d.update(k)
    return d


def main():
    owner, member = C(), C()
    if not owner.register("demo"):
        print("User 'demo' already exists - nothing to do. (Delete the data/ folder to start fresh.)")
        return
    member.register("amit_viewer")
    pid = owner.call("POST", "/api/projects", {"name": "Library", "description": "Books, authors, members and loans"})["project_id"]
    u = f"/api/p/{pid}"
    P = lambda path, body: owner.call("POST", u + path, body)
    P("/tables", {"name": "author", "columns": [col("author_id", "INT", pk=True, auto=True), col("name", length=60, notnull=True), col("country", length=40)]})
    P("/tables", {"name": "category", "columns": [col("category_id", "INT", pk=True, auto=True), col("cname", length=40, notnull=True, unique=True)]})
    P("/tables", {"name": "book", "audit": True, "columns": [
        col("book_id", "INT", pk=True, auto=True), col("title", length=100, notnull=True),
        col("price", "DECIMAL", precision=8, scale=2, check={"op": ">=", "value": "0"}), col("published", "DATE"),
        col("in_stock", "BOOLEAN", default="1"),
        col("author_id", "INT", notnull=True, fk={"table": "author", "column": "author_id", "on_delete": "CASCADE"}),
        col("category_id", "INT", fk={"table": "category", "column": "category_id", "on_delete": "SET NULL"})]})
    P("/tables", {"name": "member", "columns": [
        col("member_id", "INT", pk=True, auto=True), col("mname", length=60, notnull=True), col("email", length=80, unique=True),
        col("city", length=40), col("referred_by", "INT", fk={"table": "member", "column": "member_id", "on_delete": "SET NULL"})]})
    P("/tables", {"name": "loan", "columns": [
        col("loan_id", "INT", pk=True, auto=True),
        col("book_id", "INT", notnull=True, fk={"table": "book", "column": "book_id", "on_delete": "CASCADE"}),
        col("member_id", "INT", notnull=True, fk={"table": "member", "column": "member_id", "on_delete": "CASCADE"}),
        col("issued", "DATE", notnull=True), col("returned", "DATE")]})
    P("/tables/book/indexes", {"name": "idx_book_title", "columns": ["title"]})
    P("/tables", {"name": "loan_flat", "columns": [col("member_id", "INT", pk=True), col("book_id", "INT", pk=True),
                                                    col("mname", length=60), col("title", length=100), col("author_name", length=60), col("issued", "DATE")]})
    P("/fds/loan_flat", {"lhs": "member_id", "rhs": "mname"})
    P("/fds/loan_flat", {"lhs": "book_id", "rhs": "title,author_name"})
    P("/fds/loan_flat", {"lhs": "member_id,book_id", "rhs": "issued"})
    for n, c in [("George Orwell", "UK"), ("Jane Austen", "UK"), ("J.R.R. Tolkien", "UK"), ("Chinua Achebe", "Nigeria"), ("R.K. Narayan", "India")]:
        P("/rows/author", {"name": n, "country": c})
    for n in ["Fiction", "Fantasy", "Classic", "Non-fiction"]:
        P("/rows/category", {"cname": n})
    for t, pr, d, s, a, c in [("1984", 9.5, "1949-06-08", 1, 1, 1), ("Animal Farm", 7, "1945-08-17", 1, 1, 1), ("Emma", 6.25, "1815-12-23", 1, 2, 3),
                              ("Pride and Prejudice", 8, "1813-01-28", 1, 2, 3), ("Persuasion", 5.5, "1817-12-20", 0, 2, 3), ("The Hobbit", 12, "1937-09-21", 1, 3, 2),
                              ("The Fellowship of the Ring", 18, "1954-07-29", 1, 3, 2), ("The Two Towers", 18.5, "1954-11-11", 1, 3, 2),
                              ("Things Fall Apart", 10, "1958-06-17", 1, 4, 3), ("Malgudi Days", 11, "1943-01-01", 1, 5, 3), ("Swami and Friends", 9, "1935-01-01", 0, 5, 3)]:
        P("/rows/book", {"title": t, "price": pr, "published": d, "in_stock": s, "author_id": a, "category_id": c})
    for n, e, c, r in [("Asha Rao", "asha@mail.com", "Jaipur", ""), ("Ben Thomas", "ben@mail.com", "Delhi", 1), ("Chitra Iyer", "chitra@mail.com", "Jaipur", 1),
                       ("Dev Patel", "dev@mail.com", "Mumbai", 2), ("Esha Khan", "esha@mail.com", "Delhi", 3), ("Farhan Ali", "farhan@mail.com", "Jaipur", "")]:
        P("/rows/member", {"mname": n, "email": e, "city": c, "referred_by": r})
    for b, m, i, r in [(1, 1, "2026-09-01", "2026-09-12"), (2, 1, "2026-09-14", ""), (6, 2, "2026-09-05", "2026-09-20"), (7, 3, "2026-09-10", ""), (8, 3, "2026-09-10", ""),
                       (9, 4, "2026-10-01", ""), (3, 5, "2026-10-03", "2026-10-10"), (4, 5, "2026-10-03", ""), (10, 6, "2026-10-05", "2026-10-15"), (6, 6, "2026-10-16", "")]:
        P("/rows/loan", {"book_id": b, "member_id": m, "issued": i, "returned": r})
    P("/views", {"name": "v_overdue", "sql": 'SELECT m.mname, b.title, l.issued FROM loan l JOIN member m ON m.member_id = l.member_id JOIN book b ON b.book_id = l.book_id WHERE l.returned IS NULL'})
    P("/views", {"name": "v_catalog", "sql": 'SELECT b.title, a.name AS author, c.cname AS category, b.price FROM book b JOIN author a ON a.author_id = b.author_id LEFT JOIN category c ON c.category_id = b.category_id'})
    P("/routines", {"name": "issue_book", "body": "INSERT INTO loan(book_id, member_id, issued) VALUES (:book_id, :member_id, :issue_date);\nUPDATE book SET in_stock = 0 WHERE book_id = :book_id;\nSELECT b.title, m.mname, l.issued FROM loan l JOIN book b ON b.book_id=l.book_id JOIN member m ON m.member_id=l.member_id WHERE l.loan_id = (SELECT MAX(loan_id) FROM loan);"})
    P("/routines", {"name": "return_book", "body": "UPDATE loan SET returned = :return_date WHERE book_id = :book_id AND returned IS NULL;\nUPDATE book SET in_stock = 1 WHERE book_id = :book_id;\nSELECT title, in_stock FROM book WHERE book_id = :book_id;"})
    P("/members", {"username": "amit_viewer"})
    P("/grant", {"username": "amit_viewer", "table": "book", "privilege": "SELECT"})
    P("/grant", {"username": "amit_viewer", "table": "v_catalog", "privilege": "SELECT"})
    P("/grant", {"username": "amit_viewer", "table": "loan", "privilege": "INSERT"})
    print("Demo ready.  Sign in as  demo / demo1234  (owner)  or  amit_viewer / demo1234  (limited member).")
    print("Start the app with:  python app.py")


if __name__ == "__main__":
    sys.exit(main())
