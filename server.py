import json
import os
import sqlite3
import uuid
from typing import Optional, List, Dict, Any
import datetime

from fastapi import FastAPI, UploadFile, File, Form, Query, HTTPException, Request, Depends
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session

import models
from database import engine, get_db, Base
from label_studio_sdk import LabelStudio
from detector import DetectionClientError, detect_objects

HOST = "127.0.0.1"
PORT = int(os.environ.get("APP_PORT", "8765"))
LABEL_STUDIO_URL = os.environ.get("LABEL_STUDIO_URL", "http://localhost:8000/")
LABEL_STUDIO_API_KEY = os.environ.get("LABEL_STUDIO_API_KEY", "")

# Create tables
Base.metadata.create_all(bind=engine)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Pydantic Models ---

class WorkspaceData(BaseModel):
    key: str
    value: str

class ProjectModel(BaseModel):
    name: str
    slug: str
    type: str = "Image - Polygon"
    creator: str

class TaskUpdate(BaseModel):
    id: Optional[int] = None
    assignee: Optional[str] = None
    status: Optional[str] = "New"
    description: Optional[str] = None
    time_spent_delta: Optional[int] = 0
    annotations: Optional[str] = None

class BulkDelete(BaseModel):
    ids: List[int]

class BulkUpdate(BaseModel):
    ids: List[int]
    assignee: Optional[str] = None
    status: Optional[str] = None

class TeamMemberModel(BaseModel):
    name: str

class TeamTime(BaseModel):
    name: str
    time_logged: int

class DetectPayload(BaseModel):
    image: str
    selection: Optional[List[dict]] = None

class LabelStudioPayload(BaseModel):
    projectId: Optional[str] = None
    taskId: Optional[str] = None
    taskData: Optional[dict] = None
    result: Optional[list] = None

# --- API Endpoints ---

@app.get("/api/data")
def get_data(db: Session = Depends(get_db)):
    rows = db.query(models.WorkspaceData).all()
    return {row.key: row.value for row in rows}

@app.post("/api/data")
def set_data(data: WorkspaceData, db: Session = Depends(get_db)):
    item = db.query(models.WorkspaceData).filter(models.WorkspaceData.key == data.key).first()
    if item:
        item.value = data.value
    else:
        item = models.WorkspaceData(key=data.key, value=data.value)
        db.add(item)
    db.commit()
    return {"status": "ok"}

@app.get("/api/projects")
def get_projects(db: Session = Depends(get_db)):
    projects = db.query(models.Project).all()
    return [{"id": p.id, "name": p.name, "slug": p.slug, "type": p.type, "status": p.status, "creator": p.creator, "created_at": p.created_at} for p in projects]

@app.get("/api/projects/{project_id}/metrics")
def get_project_metrics(project_id: int, db: Session = Depends(get_db)):
    total = db.query(models.Task).filter(models.Task.project_id == project_id).count()
    completed = db.query(models.Task).filter(models.Task.project_id == project_id, models.Task.status == 'Completed').count()
    
    progress = int((completed / total * 100)) if total > 0 else 0
    
    project = db.query(models.Project).filter(models.Project.id == project_id).first()
    if project:
        if total > 0 and completed == total:
            project.status = 'Completed'
            db.commit()
        elif completed > 0:
            project.status = 'In Progress'
            db.commit()

    return {"total": total, "completed": completed, "progress": progress}

@app.post("/api/projects")
def create_project(project: ProjectModel, db: Session = Depends(get_db)):
    db_project = models.Project(name=project.name, slug=project.slug, type=project.type, status="Preparing", creator=project.creator)
    db.add(db_project)
    db.commit()
    db.refresh(db_project)
    return {"id": db_project.id, "status": "ok"}

@app.post("/api/projects/{project_id}/upload")
def upload_files(project_id: int, assignee: Optional[str] = Query(None), file: List[UploadFile] = File(...), db: Session = Depends(get_db)):
    os.makedirs("uploads", exist_ok=True)
    saved_files = []
    
    ALLOWED_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.webp'}
    
    for f in file:
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise HTTPException(status_code=400, detail=f"File type {ext} is not allowed.")
            
        new_filename = f"{uuid.uuid4().hex}{ext}"
        filepath = os.path.join("uploads", new_filename)
        
        with open(filepath, "wb") as out_file:
            out_file.write(f.file.read())
            
        task = models.Task(project_id=project_id, image_path=filepath, description=f.filename, status='New', assignee=assignee)
        db.add(task)
        saved_files.append(filepath)
        
    db.commit()
    return {"status": "ok", "files": saved_files}

@app.get("/api/tasks")
def get_tasks(projectId: Optional[int] = Query(None), db: Session = Depends(get_db)):
    if projectId:
        tasks = db.query(models.Task).filter(models.Task.project_id == projectId).all()
    else:
        tasks = db.query(models.Task).all()
    
    result = []
    for t in tasks:
        annotations_data = []
        if t.annotations:
            try:
                annotations_data = json.loads(t.annotations)
            except:
                pass
        result.append({
            "id": t.id, "description": t.description, "assignee": t.assignee, 
            "image_path": t.image_path, "status": t.status, "time_spent": t.time_spent, 
            "updated_at": t.updated_at, "annotations": annotations_data
        })
    return result

@app.post("/api/tasks")
def update_or_create_task(task: TaskUpdate, projectId: Optional[int] = Query(None), db: Session = Depends(get_db)):
    if task.id:
        db_task = db.query(models.Task).filter(models.Task.id == task.id).first()
        if db_task:
            if task.assignee is not None:
                db_task.assignee = task.assignee
            if task.status is not None:
                db_task.status = task.status
            if task.description is not None:
                db_task.description = task.description
            if task.time_spent_delta is not None:
                db_task.time_spent = (db_task.time_spent or 0) + task.time_spent_delta
            if task.annotations is not None:
                db_task.annotations = task.annotations
            db_task.updated_at = datetime.datetime.utcnow()
            task_id = db_task.id
        else:
            raise HTTPException(status_code=404, detail="Task not found")
    else:
        db_task = models.Task(
            description=task.description, 
            assignee=task.assignee, 
            project_id=projectId, 
            status=task.status or "New", 
            time_spent=task.time_spent_delta or 0, 
            annotations=task.annotations,
            updated_at=datetime.datetime.utcnow()
        )
        db.add(db_task)
        db.commit()
        db.refresh(db_task)
        task_id = db_task.id
        
    db.commit()
    return {"id": task_id, "status": "ok"}

@app.delete("/api/tasks/{task_id}")
def delete_task(task_id: int, db: Session = Depends(get_db)):
    db.query(models.Task).filter(models.Task.id == task_id).delete()
    db.commit()
    return {"status": "ok"}

@app.post("/api/tasks/bulk-delete")
def bulk_delete_tasks(payload: BulkDelete, db: Session = Depends(get_db)):
    if not payload.ids:
        raise HTTPException(status_code=400, detail="No ids provided")
    db.query(models.Task).filter(models.Task.id.in_(payload.ids)).delete(synchronize_session=False)
    db.commit()
    return {"status": "ok"}

@app.post("/api/tasks/bulk-update")
def bulk_update_tasks(payload: BulkUpdate, db: Session = Depends(get_db)):
    if not payload.ids:
        raise HTTPException(status_code=400, detail="No ids provided")
    
    update_data = {}
    if payload.assignee is not None:
        update_data[models.Task.assignee] = payload.assignee
    if payload.status is not None:
        update_data[models.Task.status] = payload.status
        
    if update_data:
        update_data[models.Task.updated_at] = datetime.datetime.utcnow()
        db.query(models.Task).filter(models.Task.id.in_(payload.ids)).update(update_data, synchronize_session=False)
        db.commit()
        
    return {"status": "ok"}

@app.get("/api/team")
def get_team(db: Session = Depends(get_db)):
    team = db.query(models.TeamMember).all()
    return [{"name": t.name, "time_logged": t.time_logged} for t in team]

@app.post("/api/team")
def create_team_member(member: TeamMemberModel, db: Session = Depends(get_db)):
    existing = db.query(models.TeamMember).filter(models.TeamMember.name == member.name).first()
    if not existing:
        new_member = models.TeamMember(name=member.name, time_logged=0)
        db.add(new_member)
        db.commit()
    return {"status": "ok"}

@app.delete("/api/team/{name}")
def delete_team_member(name: str, db: Session = Depends(get_db)):
    import urllib.parse
    name = urllib.parse.unquote(name)
    db.query(models.TeamMember).filter(models.TeamMember.name == name).delete()
    db.commit()
    return {"status": "ok"}

@app.post("/api/team/time")
def update_team_time(payload: TeamTime, db: Session = Depends(get_db)):
    member = db.query(models.TeamMember).filter(models.TeamMember.name == payload.name).first()
    if member:
        member.time_logged = payload.time_logged
        db.commit()
    return {"status": "ok"}

@app.post("/api/detect")
def detect(payload: DetectPayload):
    try:
        response = detect_objects(payload.image, selection=payload.selection)
        return response
    except DetectionClientError as error:
        raise HTTPException(status_code=400, detail=str(error))
    except Exception:
        raise HTTPException(status_code=500, detail="Object detection failed.")

@app.post("/api/label-studio/send")
def send_to_ls(payload: LabelStudioPayload):
    if not LABEL_STUDIO_API_KEY:
        raise HTTPException(status_code=400, detail="Set LABEL_STUDIO_API_KEY before starting server.py.")
    if not payload.taskId and not payload.projectId:
        raise HTTPException(status_code=400, detail="Send projectId to create a task, or taskId to annotate an existing task.")
    if not payload.taskData:
        raise HTTPException(status_code=400, detail="Missing taskData.")
    if not payload.result:
        raise HTTPException(status_code=400, detail="Missing annotation result.")

    try:
        client = LabelStudio(
            base_url=LABEL_STUDIO_URL,
            api_key=LABEL_STUDIO_API_KEY,
        )

        task_id = payload.taskId
        if not task_id:
            task = client.tasks.create(data=payload.taskData, project=int(payload.projectId))
            task_id = str(task.id)

        annotation = client.annotations.create(
            int(task_id),
            result=payload.result,
            was_cancelled=False,
            ground_truth=False,
        )

        return {
            "taskId": task_id,
            "annotationId": annotation.id,
            "labelStudioUrl": LABEL_STUDIO_URL,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail="Label Studio sync failed.")

# --- Static Files ---

@app.get("/")
def read_index():
    return FileResponse("index.html")

@app.get("/{filename}.html")
def read_html(filename: str):
    return FileResponse(f"{filename}.html")

@app.get("/{filename}.js")
def read_js(filename: str):
    return FileResponse(f"{filename}.js")

@app.get("/{filename}.css")
def read_css(filename: str):
    return FileResponse(f"{filename}.css")

@app.get("/{filename}.png")
def read_png(filename: str):
    return FileResponse(f"{filename}.png")

app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

if __name__ == "__main__":
    import uvicorn
    print(f"App running at http://{HOST}:{PORT}/")
    print(f"Label Studio target: {LABEL_STUDIO_URL}")
    print("Object detection: YOLOv8 ONNX via OpenCV DNN")
    uvicorn.run("server:app", host=HOST, port=PORT, reload=False)
