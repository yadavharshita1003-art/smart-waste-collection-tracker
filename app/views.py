from datetime import datetime, date
import uuid
import pymysql
from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for
from werkzeug.security import generate_password_hash
from .db import execute, insert, query, query_one
from .errors import AppError, user_message
from .security import requires

bp=Blueprint("views",__name__)
BIN_TYPES=["wet","dry","recyclable"]; SHIFTS=["morning","afternoon","night"]; ZONE_TYPES=["city","region","ward"]; STATION_TYPES=["transfer","landfill","recycling"]; RESULTS=["scheduled","collected","missed"]; VEHICLE_STATUSES=["active","idle","maintenance"]; ROLES=["admin","supervisor","worker"]
def f_text(n,max_len=200,required=False,label=None):
 v=request.form.get(n,"").strip()[:max_len]
 if required and not v: raise AppError(f"{label or n} is required.")
 return v or None
def f_int(n,label=None,required=True):
 v=request.form.get(n,"").strip()
 if not v:
  if required: raise AppError(f"{label or n} is required.")
  return None
 try:return int(v)
 except ValueError: raise AppError(f"{label or n} must be a whole number.")
def f_float(n,label=None,minimum=None):
 try:v=float(request.form.get(n,""))
 except ValueError:raise AppError(f"{label or n} must be a number.")
 if minimum is not None and v<minimum:raise AppError(f"{label or n} must be at least {minimum}.")
 return v
def f_date(n,label=None):
 try:return datetime.strptime(request.form.get(n,""),"%Y-%m-%d").date()
 except ValueError:raise AppError(f"{label or n} is not a valid date.")
def f_choice(n,opts,label=None):
 v=request.form.get(n,"").strip()
 if v not in opts:raise AppError(f"Choose a valid {label or n}.")
 return v
def attempt(fn,endpoint):
 try:flash(fn() or "Saved.","ok")
 except AppError as e:flash(str(e),"error")
 except pymysql.MySQLError as e:current_app.logger.exception("Database change failed");flash(user_message(e),"error")
 return redirect(url_for(endpoint))
def acting_worker_id(label="Worker"):
 u=session["user"]
 if u["role"]=="worker":
  if u.get("worker_id") is None:raise AppError("Your account is not linked to a worker record.")
  return u["worker_id"]
 return f_int("worker_id",label)
def key(prefix): return prefix+uuid.uuid4().hex[:7].upper()
@bp.route("/")
@requires(page="overview")
def index():return redirect(url_for("views.overview"))
@bp.route("/overview")
@requires(page="overview")
def overview():
 stats=query_one("SELECT (SELECT COUNT(*) FROM bin) bins,(SELECT ROUND(100*SUM(status='collected')/NULLIF(COUNT(*),0),1) FROM collection_event) success_rate,(SELECT COUNT(*) FROM complaint WHERE status='open') open_complaints,(SELECT COUNT(*) FROM vehicle WHERE current_status='active') in_transit") or {}
 zones=query("SELECT z.zone_id,z.name,z.zone_type,ROUND(100*SUM(ce.status='collected')/NULLIF(COUNT(ce.bin_id),0),1) collection_pct FROM zone z LEFT JOIN bin b ON b.zone_id=z.zone_id LEFT JOIN collection_event ce ON ce.bin_id=b.bin_id WHERE z.zone_type IN ('city','region') GROUP BY z.zone_id,z.name,z.zone_type ORDER BY z.zone_id")
 batches=query("SELECT b.batch_id,b.route_id,b.date AS batch_date,b.disposed_at,v.vehicle_type,ROUND(100*b.total_weight/NULLIF(v.capacity,0),1) utilization FROM batch b JOIN vehicle v ON v.vehicle_id=b.vehicle_id ORDER BY b.date DESC LIMIT 8")
 events=query("SELECT ce.event_timestamp,ce.bin_id,ce.status,w.name worker_name FROM collection_event ce JOIN worker w ON w.worker_id=ce.worker_id ORDER BY ce.event_timestamp DESC LIMIT 8")
 return render_template("overview.html",stats=stats,zones=zones,batches=batches,events=events)
@bp.route("/batches")
@requires(page="batches")
def batches():
 rows=query("SELECT b.batch_id,b.route_id,b.date AS batch_date,b.total_weight,b.disposed_at,v.vehicle_type,v.vehicle_id,s.name station_name,ROUND(100*b.total_weight/NULLIF(v.capacity,0),1) utilization FROM batch b JOIN vehicle v ON v.vehicle_id=b.vehicle_id LEFT JOIN transfer_station s ON s.station_id=b.station_id ORDER BY b.date DESC")
 return render_template("batches.html",batches=rows,vehicles=query("SELECT vehicle_id,vehicle_type,current_status FROM vehicle ORDER BY vehicle_id"),stations=query("SELECT station_id,name,station_type FROM transfer_station ORDER BY station_id"))
@bp.route("/batches/new",methods=["POST"])
@requires(action="batch")
def batch_new():
 def do():
  vid=f_text("vehicle_id",20,True,"Vehicle"); route=f_text("route_id",20); d=f_date("batch_date","Batch date"); bid=key("BAT")
  insert("INSERT INTO batch(batch_id,vehicle_id,route_id,date,total_weight,station_id) VALUES(%(id)s,%(v)s,%(r)s,%(d)s,0,(SELECT station_id FROM transfer_station ORDER BY station_id LIMIT 1))",{"id":bid,"v":vid,"r":route,"d":d})
  return f"Batch {bid} created."
 return attempt(do,"views.batches")
@bp.route("/batches/<batch_id>/close",methods=["POST"])
@requires(action="batch")
def batch_close(batch_id):
 def do():
  sid=f_int("station_id","Station"); weight=f_float("weight","Weight",0)
  if execute("UPDATE batch SET station_id=%(s)s,total_weight=%(w)s,disposed_at=NOW() WHERE batch_id=%(b)s AND disposed_at IS NULL",{"s":sid,"w":weight,"b":batch_id})==0:raise AppError("Batch not found or already closed.")
  return f"Batch {batch_id} closed."
 return attempt(do,"views.batches")
@bp.route("/collections")
@requires(page="collections")
def collections():
 rows=query("SELECT ce.bin_id,ce.event_timestamp,ce.batch_id,ce.status,ce.remarks,w.name worker_name,b.bin_type,z.name zone_name FROM collection_event ce JOIN worker w ON w.worker_id=ce.worker_id JOIN bin b ON b.bin_id=ce.bin_id JOIN zone z ON z.zone_id=b.zone_id ORDER BY ce.event_timestamp DESC LIMIT 100")
 return render_template("collections.html",rows=rows,bins=query("SELECT bin_id,bin_type FROM bin ORDER BY bin_id"),open_batches=query("SELECT batch_id,route_id FROM batch WHERE disposed_at IS NULL ORDER BY date DESC"),workers=query("SELECT worker_id,name FROM worker ORDER BY name"))
@bp.route("/collections/new",methods=["POST"])
@requires(action="collection")
def collection_new():
 def do():
  bid=f_text("bin_id",10,True,"Bin"); batch=f_text("batch_id",20,True,"Batch"); status=f_choice("status",RESULTS,"result"); remarks=f_text("remarks",200); wid=acting_worker_id()
  insert("INSERT INTO collection_event(bin_id,event_timestamp,worker_id,batch_id,status,remarks) VALUES(%(b)s,NOW(),%(w)s,%(ba)s,%(s)s,%(r)s)",{"b":bid,"w":wid,"ba":batch,"s":status,"r":remarks})
  return "Collection recorded."
 return attempt(do,"views.collections")
@bp.route("/complaints")
@requires(page="complaints")
def complaints():
 rows=query("SELECT c.bin_id,c.complaint_seq_no,c.date_raised,c.description,c.status,w.name resolved_name,z.name zone_name FROM complaint c JOIN bin b ON b.bin_id=c.bin_id JOIN zone z ON z.zone_id=b.zone_id LEFT JOIN worker w ON w.worker_id=c.resolved_by ORDER BY (c.status='open') DESC,c.date_raised DESC,c.bin_id")
 return render_template("complaints.html",rows=rows,bins=query("SELECT bin_id FROM bin ORDER BY bin_id"),workers=query("SELECT worker_id,name FROM worker ORDER BY name"))
@bp.route("/complaints/new",methods=["POST"])
@requires(action="complaint")
def complaint_new():
 def do():
  bid=f_text("bin_id",10,True,"Bin"); desc=f_text("description",500,True,"Description"); row=query_one("SELECT COALESCE(MAX(complaint_seq_no),0)+1 n FROM complaint WHERE bin_id=%(b)s",{"b":bid}); seq=row["n"]
  execute("INSERT INTO complaint(bin_id,complaint_seq_no,date_raised,description,status) VALUES(%(b)s,%(s)s,CURDATE(),%(d)s,'open')",{"b":bid,"s":seq,"d":desc}); return f"Complaint #{seq} raised for bin {bid}."
 return attempt(do,"views.complaints")
@bp.route("/complaints/<bin_id>/<int:seq>/resolve",methods=["POST"])
@requires(action="complaint")
def complaint_resolve(bin_id,seq):
 def do():
  wid=acting_worker_id("Resolved by"); n=execute("UPDATE complaint SET status='resolved',resolved_by=%(w)s WHERE bin_id=%(b)s AND complaint_seq_no=%(s)s",{"w":wid,"b":bin_id,"s":seq})
  if not n:raise AppError("Complaint not found.")
  return "Complaint resolved."
 return attempt(do,"views.complaints")
@bp.route("/bins")
@requires(page="bins")
def bins():
 rows=query("SELECT b.bin_id,b.bin_type,b.latitude,b.longitude,b.household_id,z.name zone_name,DATEDIFF(CURDATE(),(SELECT MAX(event_timestamp) FROM collection_event ce WHERE ce.bin_id=b.bin_id)) days_since_collection,(SELECT COUNT(*) FROM complaint c WHERE c.bin_id=b.bin_id AND c.status<>'resolved') open_complaints FROM bin b JOIN zone z ON z.zone_id=b.zone_id ORDER BY b.bin_id")
 return render_template("bins.html",rows=rows,streets=query("SELECT zone_id,name FROM zone WHERE zone_type='ward' ORDER BY name"))
@bp.route("/bins/new",methods=["POST"])
@requires(action="master")
def bin_new():
 def do():
  bid=key("BIN"); typ=f_choice("bin_type",BIN_TYPES,"bin type"); lat=f_float("latitude","Latitude"); lon=f_float("longitude","Longitude"); hh=f_text("household_id",30); zid=f_text("zone_id",10,True,"Zone")
  insert("INSERT INTO bin(bin_id,bin_type,latitude,longitude,household_id,zone_id) VALUES(%(b)s,%(t)s,%(la)s,%(lo)s,%(h)s,%(z)s)",{"b":bid,"t":typ,"la":lat,"lo":lon,"h":hh,"z":zid}); return f"Bin {bid} added."
 return attempt(do,"views.bins")
@bp.route("/bins/<bin_id>/delete",methods=["POST"])
@requires(action="master")
def bin_delete(bin_id):
 def do():
  if execute("DELETE FROM bin WHERE bin_id=%(b)s",{"b":bin_id})==0:raise AppError("Bin not found.")
  return f"Bin {bin_id} deleted."
 return attempt(do,"views.bins")
@bp.route("/vehicles")
@requires(page="vehicles")
def vehicles():
 return render_template("vehicles.html",rows=query("SELECT v.vehicle_id,v.vehicle_type,v.capacity capacity_kg,v.current_status,GROUP_CONCAT(w.name ORDER BY w.name SEPARATOR ', ') crew FROM vehicle v LEFT JOIN worker w ON w.assigned_vehicle_id=v.vehicle_id GROUP BY v.vehicle_id,v.vehicle_type,v.capacity,v.current_status ORDER BY v.vehicle_id"))
@bp.route("/vehicles/new",methods=["POST"])
@requires(action="master")
def vehicle_new():
 def do():
  typ=f_choice("vehicle_type",["compactor","tipper","mini-truck"],"vehicle type"); cap=f_float("capacity_kg","Capacity",1); vid=key("VEH")
  insert("INSERT INTO vehicle(vehicle_id,capacity,vehicle_type,current_status) VALUES(%(v)s,%(c)s,%(t)s,'idle')",{"v":vid,"c":cap,"t":typ});return f"Vehicle {vid} added."
 return attempt(do,"views.vehicles")
@bp.route("/vehicles/<vehicle_id>/status",methods=["POST"])
@requires(action="vehicle")
def vehicle_status(vehicle_id):
 def do():
  status=f_choice("status",VEHICLE_STATUSES,"status")
  if query_one("SELECT batch_id FROM batch WHERE vehicle_id=%(v)s AND disposed_at IS NULL LIMIT 1",{"v":vehicle_id}):raise AppError("Close the open batch before changing this vehicle.")
  if not execute("UPDATE vehicle SET current_status=%(s)s WHERE vehicle_id=%(v)s",{"s":status,"v":vehicle_id}):raise AppError("Vehicle not found.")
  return "Vehicle status updated."
 return attempt(do,"views.vehicles")
@bp.route("/workers")
@requires(page="workers")
def workers():
 return render_template("workers.html",rows=query("SELECT w.worker_id,w.name,w.phone,w.shift,w.assigned_vehicle_id,v.vehicle_type FROM worker w LEFT JOIN vehicle v ON v.vehicle_id=w.assigned_vehicle_id ORDER BY w.worker_id"),vehicles=query("SELECT vehicle_id,vehicle_type FROM vehicle ORDER BY vehicle_id"))
@bp.route("/workers/new",methods=["POST"])
@requires(action="master")
def worker_new():
 def do():
  name=f_text("name",100,True,"Name"); phone=f_text("phone",15); shift=f_choice("shift",SHIFTS,"shift"); vid=f_text("vehicle_id",20)
  insert("INSERT INTO worker(name,phone,shift,assigned_vehicle_id) VALUES(%(n)s,%(p)s,%(s)s,%(v)s)",{"n":name,"p":phone,"s":shift,"v":vid});return "Worker added."
 return attempt(do,"views.workers")
@bp.route("/workers/<int:worker_id>/vehicle",methods=["POST"])
@requires(action="master")
def worker_vehicle(worker_id):
 def do():
  vid=f_text("vehicle_id",20)
  if not execute("UPDATE worker SET assigned_vehicle_id=%(v)s WHERE worker_id=%(w)s",{"v":vid,"w":worker_id}):raise AppError("Worker not found.")
  return "Worker reassigned."
 return attempt(do,"views.workers")
@bp.route("/zones")
@requires(page="zones")
def zones():
 return render_template("zones.html",rows=query("SELECT z.zone_id,z.name,z.zone_type,p.name parent_name,(SELECT COUNT(*) FROM bin b WHERE b.zone_id=z.zone_id) bin_count FROM zone z LEFT JOIN zone p ON p.zone_id=z.parent_zone_id ORDER BY z.zone_id"),parents=query("SELECT zone_id,name FROM zone ORDER BY name"))
@bp.route("/zones/new", methods=["POST"])
@requires(action="master")
def zone_new():
    def do():
        zid = f_text("zone_id", 10, True, "Zone ID")
        name = f_text("name", 100, True, "Zone name")
        parent = f_text("parent_zone_id", 10) or None
        typ = f_choice("zone_type", ZONE_TYPES, "zone type")

        if parent == zid:
            raise AppError("A zone cannot be its own parent.")

        if parent and not query_one(
            "SELECT zone_id FROM zone WHERE zone_id=%(p)s",
            {"p": parent},
        ):
            raise AppError("Selected parent zone does not exist.")

        insert(
            "INSERT INTO zone (zone_id, name, parent_zone_id, zone_type) "
            "VALUES (%(id)s, %(n)s, %(p)s, %(t)s)",
            {"id": zid, "n": name, "p": parent, "t": typ},
        )
        return f"Zone {zid} added."

    return attempt(do, "views.zones")
@bp.route("/stations")
@requires(page="stations")
def stations():
 rows=query("SELECT s.station_id,s.name,s.station_type,s.capacity capacity_kg,s.latitude,s.longitude,COUNT(b.batch_id) batches_received FROM transfer_station s LEFT JOIN batch b ON b.station_id=s.station_id GROUP BY s.station_id,s.name,s.station_type,s.capacity,s.latitude,s.longitude ORDER BY s.station_id")
 return render_template("stations.html",rows=rows)
@bp.route("/stations/new",methods=["POST"])
@requires(action="master")
def station_new():
 def do():
  name=f_text("name",100,True,"Station name");lat=f_float("latitude","Latitude");lon=f_float("longitude","Longitude");cap=f_float("capacity_kg","Capacity",1);typ=f_choice("station_type",STATION_TYPES,"station type");insert("INSERT INTO transfer_station(name,latitude,longitude,capacity,station_type) VALUES(%(n)s,%(la)s,%(lo)s,%(c)s,%(t)s)",{"n":name,"la":lat,"lo":lon,"c":cap,"t":typ});return "Station added."
 return attempt(do,"views.stations")
@bp.route("/users")
@requires(page="users")
def users():
 return render_template("users.html",rows=query("SELECT u.user_id,u.username,u.full_name,u.role,w.name worker_name FROM app_user u LEFT JOIN worker w ON w.worker_id=u.worker_id ORDER BY u.username"),workers=query("SELECT worker_id,name FROM worker ORDER BY name"),roles=ROLES)
@bp.route("/users/new",methods=["POST"])
@requires(action="users")
def user_new():
 def do():
  u=f_text("username",50,True,"Username"); name=f_text("full_name",100,True,"Full name");pw=request.form.get("password","")
  if len(pw)<8:raise AppError("Password must be at least 8 characters.")
  role=f_choice("role",ROLES,"role");wid=f_int("worker_id","Linked worker",False)
  if role=="worker" and wid is None:raise AppError("Worker accounts must be linked to a worker record.")
  insert("INSERT INTO app_user(username,password_hash,full_name,role,worker_id) VALUES(%(u)s,%(p)s,%(n)s,%(r)s,%(w)s)",{"u":u,"p":generate_password_hash(pw),"n":name,"r":role,"w":wid});return f"User {u} created."
 return attempt(do,"views.users")
