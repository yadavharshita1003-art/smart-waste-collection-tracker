import click
from werkzeug.security import generate_password_hash
from .db import execute, query_one
def register_cli(app):
 @app.cli.command("seed-users")
 def seed_users():
  """Create demo logins if missing. Change these passwords after first login."""
  users=[("admin","admin123","Admin","admin",None),("supervisor","super123","Sana Pillai","supervisor",None)]
  worker=query_one("SELECT worker_id FROM worker ORDER BY worker_id LIMIT 1")
  if worker: users.append(("worker","worker123","Worker","worker",worker["worker_id"]))
  for u,p,n,r,w in users:
   if query_one("SELECT user_id FROM app_user WHERE username=%(u)s",{"u":u}): click.echo("exists: "+u); continue
   execute("INSERT INTO app_user(username,password_hash,full_name,role,worker_id) VALUES(%(u)s,%(p)s,%(n)s,%(r)s,%(w)s)",{"u":u,"p":generate_password_hash(p),"n":n,"r":r,"w":w})
   click.echo("created: "+u)
