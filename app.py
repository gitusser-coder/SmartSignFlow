from flask import Flask
from editor import editor_bp

def create_app():
    app = Flask(__name__)
    app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024  # 20 MB Upload-Limit
    app.register_blueprint(editor_bp, url_prefix="/")
    return app

app = create_app()
