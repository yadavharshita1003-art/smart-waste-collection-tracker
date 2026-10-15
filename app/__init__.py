
from datetime import date

import pymysql
from flask import Flask, render_template, session

from config import Config
from . import db
from .security import csrf_token, can, nav_items


def create_app(config_object=Config):
    app = Flask(__name__)
    app.config.from_object(config_object)

    app.teardown_appcontext(db.close_conn)

    # Safe additive setup. Preserve all existing project data.
    with app.app_context():
        conn = db.get_conn()

        try:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS app_user (
                        user_id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                        username VARCHAR(50) NOT NULL UNIQUE,
                        password_hash VARCHAR(255) NOT NULL,
                        full_name VARCHAR(100) NOT NULL,
                        role ENUM(
                            'admin',
                            'supervisor',
                            'worker',
                            'citizen'
                        ) NOT NULL,
                        worker_id INT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        CONSTRAINT fk_app_user_worker
                            FOREIGN KEY (worker_id)
                            REFERENCES worker(worker_id)
                            ON DELETE SET NULL
                    ) ENGINE=InnoDB
                """)

                # Existing tables are not dropped or recreated.
                # Ensure the role column supports citizen accounts.
                cur.execute("""
                    ALTER TABLE app_user
                    MODIFY COLUMN role
                    ENUM(
                        'admin',
                        'supervisor',
                        'worker',
                        'citizen'
                    ) NOT NULL
                """)

            conn.commit()

        except Exception:
            conn.rollback()
            raise

        finally:
            db.close_conn()

    from .auth import bp as auth_bp
    from .views import bp as views_bp
    from .cli import register_cli

    app.register_blueprint(auth_bp)
    app.register_blueprint(views_bp)

    register_cli(app)

    app.jinja_env.globals.update(
        csrf_token=csrf_token,
        can=can,
        today=lambda: date.today().isoformat(),
    )

    app.jinja_env.filters["dt"] = (
        lambda v: v.strftime("%d %b %Y, %H:%M") if v else "—"
    )

    app.jinja_env.filters["dd"] = (
        lambda v: v.strftime("%d %b %Y") if v else "—"
    )

    @app.context_processor
    def inject_globals():
        return {
            "user": session.get("user"),
            "nav": nav_items(),
        }

    @app.errorhandler(pymysql.MySQLError)
    def database_error(exc):
        app.logger.error("Database error: %s", exc)

        return render_template(
            "error.html",
            message="The database is unavailable or rejected the request.",
        ), 503

    return app
