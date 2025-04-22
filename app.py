import os
from flask import Flask, render_template, request, jsonify, redirect, url_for
import pyodbc
from dotenv import load_dotenv
from math import ceil

load_dotenv()  # loads DB_SERVER, DB_USER, DB_PASSWORD from .env
SERVER = os.getenv("DB_SERVER")
USER = os.getenv("DB_USER")
PASSWORD = os.getenv("DB_PASSWORD")
DRIVER = "{ODBC Driver 17 for SQL Server}"
SYSTEM_DBS = ("master", "tempdb", "model", "msdb")

# Define system schemas to filter out by default
SYSTEM_SCHEMAS = (
    "sys",
    "INFORMATION_SCHEMA",
    "db_accessadmin",
    "db_backupoperator",
    "db_datareader",
    "db_datawriter",
    "db_ddladmin",
    "db_denydatareader",
    "db_denydatawriter",
    "db_owner",
    "db_securityadmin",
    "guest",
)

ITEMS_PER_PAGE = 20


def get_connection(db=None):
    parts = [
        f"DRIVER={DRIVER}",
        f"SERVER={SERVER}",
        f"UID={USER}",
        f"PWD={PASSWORD}",
    ]
    if db:
        parts.append(f"DATABASE={db}")
    return pyodbc.connect(";".join(parts))


def list_databases():
    with get_connection("master").cursor() as cur:
        cur.execute(
            """
            SELECT name FROM sys.databases
             WHERE state = 0
               AND name NOT IN (?,?,?,?)
             ORDER BY name
        """,
            SYSTEM_DBS,
        )
        return [r.name for r in cur.fetchall()]


def list_schemas(database, include_system_schemas=False):
    """Get all schemas in a database"""
    schemas = []
    try:
        with get_connection(database).cursor() as cur:
            if include_system_schemas:
                cur.execute(
                    """
                    SELECT DISTINCT schema_name
                    FROM information_schema.schemata
                    ORDER BY schema_name
                    """
                )
            else:
                # Exclude system schemas - using parameterized query
                placeholders = ",".join(["?"] * len(SYSTEM_SCHEMAS))
                query = f"""
                    SELECT DISTINCT schema_name
                    FROM information_schema.schemata
                    WHERE schema_name NOT IN ({placeholders})
                    ORDER BY schema_name
                """
                cur.execute(query, SYSTEM_SCHEMAS)

            schemas = [r.schema_name for r in cur.fetchall()]
    except Exception as e:
        print(f"Error listing schemas: {e}")
    return schemas


def is_system_schema(schema_name):
    """Check if a schema is a system schema"""
    return schema_name in SYSTEM_SCHEMAS


def list_foreign_keys(cursor, schema, table):
    fk_sql = """
    SELECT
      fk.name               AS constraint_name,
      c1.name               AS column_name,
      SCHEMA_NAME(t2.schema_id) AS referenced_schema,
      t2.name               AS referenced_table,
      c2.name               AS referenced_column
    FROM sys.foreign_keys fk
    JOIN sys.foreign_key_columns fkc 
      ON fk.object_id = fkc.constraint_object_id
    JOIN sys.tables t1
      ON fk.parent_object_id = t1.object_id
    JOIN sys.schemas s1
      ON t1.schema_id = s1.schema_id
    JOIN sys.columns c1
      ON fkc.parent_object_id = c1.object_id 
     AND fkc.parent_column_id = c1.column_id
    JOIN sys.tables t2
      ON fk.referenced_object_id = t2.object_id
    JOIN sys.columns c2
      ON fkc.referenced_object_id = c2.object_id 
     AND fkc.referenced_column_id = c2.column_id
    WHERE s1.name = ? AND t1.name = ?
    ORDER BY fk.name, fkc.constraint_column_id
    """
    cursor.execute(fk_sql, (schema, table))
    fks = []
    for row in cursor.fetchall():
        fks.append(
            {
                "constraint": row.constraint_name,
                "column": row.column_name,
                "referenced_table": f"{row.referenced_schema}.{row.referenced_table}",
                "referenced_column": row.referenced_column,
            }
        )
    return fks


def list_objects_by_schema(database, schema=None):
    """Get all objects in a database and schema"""
    meta = []
    try:
        conn = get_connection(database)
        cur = conn.cursor()

        schema_filter = ""
        params = []
        if schema:
            schema_filter = "AND TABLE_SCHEMA = ?"
            params = [schema]

        # — Tables —
        cur.execute(
            f"""
            SELECT TABLE_SCHEMA, TABLE_NAME
              FROM INFORMATION_SCHEMA.TABLES
             WHERE TABLE_TYPE='BASE TABLE'
               {schema_filter}
             ORDER BY TABLE_SCHEMA, TABLE_NAME
        """,
            params,
        )
        for schema, name in cur.fetchall():
            # columns
            cur2 = conn.cursor()
            cur2.execute(
                """
                SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH,
                       IS_NULLABLE, COLUMN_DEFAULT
                  FROM INFORMATION_SCHEMA.COLUMNS
                 WHERE TABLE_SCHEMA=? AND TABLE_NAME=?
                 ORDER BY ORDINAL_POSITION
            """,
                (schema, name),
            )
            cols = []
            for col in cur2.fetchall():
                cols.append(
                    {
                        "name": col.COLUMN_NAME,
                        "type": col.DATA_TYPE,
                        "length": col.CHARACTER_MAXIMUM_LENGTH,
                        "nullable": col.IS_NULLABLE,
                        "default": col.COLUMN_DEFAULT,
                    }
                )

            # foreign keys
            fks = list_foreign_keys(cur, schema, name)

            # primary key
            cur2.execute(
                """
                SELECT column_name 
                FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE 
                WHERE OBJECTPROPERTY(OBJECT_ID(constraint_name), 'IsPrimaryKey') = 1
                AND table_schema = ? AND table_name = ?
                """,
                (schema, name),
            )
            pk_cols = [row.column_name for row in cur2.fetchall()]

            meta.append(
                {
                    "database": database,
                    "obj_type": "Table",
                    "schema": schema,
                    "name": name,
                    "columns": [c["name"] for c in cols],  # For backward compatibility
                    "column_details": cols,
                    "primary_key": pk_cols,
                    "foreign_keys": fks,
                    "is_system": is_system_schema(schema),
                }
            )

        # — Views —
        cur.execute(
            f"""
            SELECT TABLE_SCHEMA, TABLE_NAME
              FROM INFORMATION_SCHEMA.VIEWS
              {schema_filter}
             ORDER BY TABLE_SCHEMA, TABLE_NAME
        """,
            params,
        )
        for schema, name in cur.fetchall():
            # columns
            cur2 = conn.cursor()
            cur2.execute(
                """
                SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, IS_NULLABLE
                  FROM INFORMATION_SCHEMA.COLUMNS
                 WHERE TABLE_SCHEMA=? AND TABLE_NAME=?
                 ORDER BY ORDINAL_POSITION
            """,
                (schema, name),
            )
            cols = []
            for col in cur2.fetchall():
                cols.append(
                    {
                        "name": col.COLUMN_NAME,
                        "type": col.DATA_TYPE,
                        "length": col.CHARACTER_MAXIMUM_LENGTH,
                        "nullable": col.IS_NULLABLE,
                    }
                )

            # Get view definition
            cur2.execute(
                """
                SELECT VIEW_DEFINITION 
                FROM INFORMATION_SCHEMA.VIEWS
                WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?
                """,
                (schema, name),
            )
            view_def = cur2.fetchone().VIEW_DEFINITION

            meta.append(
                {
                    "database": database,
                    "obj_type": "View",
                    "schema": schema,
                    "name": name,
                    "columns": [c["name"] for c in cols],  # For backward compatibility
                    "column_details": cols,
                    "view_definition": view_def,
                    "foreign_keys": [],  # views don't have FKs
                    "is_system": is_system_schema(schema),
                }
            )

        conn.close()
    except Exception as e:
        print(f"Error listing objects: {e}")
    return meta


def get_object_details(database, schema, name):
    """Get detailed information about a specific object"""
    objects = list_objects_by_schema(database, schema)
    return next(
        (obj for obj in objects if obj["schema"] == schema and obj["name"] == name),
        None,
    )


def get_sample_data(db, schema, table, limit=5):
    try:
        conn = get_connection(db)
        cur = conn.cursor()
        # Using parametrized identifiers is tricky in SQL Server
        # This is a simplified version - in production, validate inputs carefully
        query = f"SELECT TOP {limit} * FROM [{schema}].[{table}]"
        cur.execute(query)

        columns = [column[0] for column in cur.description]
        results = []
        for row in cur:
            results.append(dict(zip(columns, row)))

        conn.close()
        return {"columns": columns, "data": results}
    except Exception as e:
        return {"error": str(e)}


# ─── FLASK SETUP ────────────────────────────────────────────────────────────
app = Flask(__name__)


@app.route("/")
def index():
    """Display list of databases"""
    databases = list_databases()

    # Get counts of objects in each database
    db_stats = {}
    for db in databases:
        try:
            conn = get_connection(db)
            cur = conn.cursor()

            # Count tables (excluding system schemas)
            placeholders = ",".join(["?"] * len(SYSTEM_SCHEMAS))
            cur.execute(
                f"""
                SELECT COUNT(*) 
                FROM INFORMATION_SCHEMA.TABLES 
                WHERE TABLE_TYPE='BASE TABLE'
                AND TABLE_SCHEMA NOT IN ({placeholders})
                """,
                SYSTEM_SCHEMAS,
            )
            table_count = cur.fetchone()[0]

            # Count views (excluding system schemas)
            cur.execute(
                f"""
                SELECT COUNT(*) 
                FROM INFORMATION_SCHEMA.VIEWS
                WHERE TABLE_SCHEMA NOT IN ({placeholders})
                """,
                SYSTEM_SCHEMAS,
            )
            view_count = cur.fetchone()[0]

            # Count user schemas (excluding system schemas)
            cur.execute(
                f"""
                SELECT COUNT(DISTINCT schema_name) 
                FROM information_schema.schemata
                WHERE schema_name NOT IN ({placeholders})
                """,
                SYSTEM_SCHEMAS,
            )
            schema_count = cur.fetchone()[0]

            db_stats[db] = {
                "tables": table_count,
                "views": view_count,
                "schemas": schema_count,
            }

            conn.close()
        except Exception as e:
            print(f"Error getting stats for {db}: {e}")
            db_stats[db] = {"tables": 0, "views": 0, "schemas": 0, "error": str(e)}

    return render_template("index.html", databases=databases, db_stats=db_stats)


@app.route("/database/<db>")
def database_view(db):
    """Display schemas in a database"""
    show_system = request.args.get("show_system", "").lower() == "true"
    schemas = list_schemas(db, include_system_schemas=show_system)

    # Get counts by schema
    schema_stats = {}
    for schema in schemas:
        try:
            conn = get_connection(db)
            cur = conn.cursor()

            # Count tables
            cur.execute(
                "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_TYPE='BASE TABLE' AND TABLE_SCHEMA=?",
                (schema,),
            )
            table_count = cur.fetchone()[0]

            # Count views
            cur.execute(
                "SELECT COUNT(*) FROM INFORMATION_SCHEMA.VIEWS WHERE TABLE_SCHEMA=?",
                (schema,),
            )
            view_count = cur.fetchone()[0]

            schema_stats[schema] = {
                "tables": table_count,
                "views": view_count,
                "is_system": is_system_schema(schema),
            }

            conn.close()
        except Exception as e:
            print(f"Error getting stats for {db}.{schema}: {e}")
            schema_stats[schema] = {
                "tables": 0,
                "views": 0,
                "is_system": is_system_schema(schema),
                "error": str(e),
            }

    return render_template(
        "database.html",
        database=db,
        schemas=schemas,
        schema_stats=schema_stats,
        show_system=show_system,
    )


@app.route("/database/<db>/schema/<schema>")
def schema_view(db, schema):
    """Display objects in a database schema"""
    q = request.args.get("q", "").lower()
    obj_type = request.args.get("type", "")
    sort_by = request.args.get("sort", "name")
    sort_dir = request.args.get("dir", "asc")
    page = int(request.args.get("page", 1))

    objects = list_objects_by_schema(db, schema)

    # Apply filters
    if q:
        objects = [
            obj
            for obj in objects
            if q in obj["name"].lower()
            or any(q in col.lower() for col in obj["columns"])
        ]

    if obj_type:
        objects = [obj for obj in objects if obj["obj_type"] == obj_type]

    # Apply sorting
    reverse = sort_dir.lower() == "desc"
    if sort_by == "name":
        objects.sort(key=lambda x: x["name"], reverse=reverse)
    elif sort_by == "type":
        objects.sort(key=lambda x: x["obj_type"], reverse=reverse)

    # Pagination
    total_items = len(objects)
    total_pages = ceil(total_items / ITEMS_PER_PAGE)
    start_idx = (page - 1) * ITEMS_PER_PAGE
    end_idx = start_idx + ITEMS_PER_PAGE
    paginated = objects[start_idx:end_idx]

    return render_template(
        "schema.html",
        database=db,
        schema=schema,
        objects=paginated,
        query=q,
        selected_type=obj_type,
        sort_by=sort_by,
        sort_dir=sort_dir,
        current_page=page,
        total_pages=total_pages,
        total_items=total_items,
        is_system=is_system_schema(schema),
    )


@app.route("/details/<db>/<schema>/<name>")
def object_details(db, schema, name):
    """Display detailed information about an object"""
    obj = get_object_details(db, schema, name)

    if not obj:
        return "Object not found", 404

    return render_template("details.html", obj=obj)


@app.route("/api/sample-data/<db>/<schema>/<table>")
def api_sample_data(db, schema, table):
    limit = int(request.args.get("limit", 5))
    data = get_sample_data(db, schema, table, limit)
    return jsonify(data)


@app.route("/api/relationships/<db>/", defaults={"schema": ""})
def api_relationships(db, schema):
    """Return relationship data for visualization"""
    try:
        # If schema is specified, get objects for that schema
        if schema and schema != "":
            objects = list_objects_by_schema(db, schema)
        else:
            # For database-level diagram, get non-system objects from all schemas
            objects = []
            schemas = list_schemas(db, include_system_schemas=False)
            for single_schema in schemas:
                schema_objects = list_objects_by_schema(db, single_schema)
                # Only include non-system objects
                objects.extend([obj for obj in schema_objects if not obj["is_system"]])

        # Filter to tables only
        db_objects = [obj for obj in objects if obj["obj_type"] == "Table"]

        # If too many tables, limit for performance
        if len(db_objects) > 75:
            db_objects = sorted(
                db_objects, key=lambda obj: len(obj["foreign_keys"]), reverse=True
            )[:75]

        nodes = []
        links = []

        # Create nodes for each table
        for obj in db_objects:
            nodes.append(
                {
                    "id": f"{obj['schema']}.{obj['name']}",
                    "schema": obj["schema"],
                    "name": obj["name"],
                    "is_system": obj["is_system"],
                }
            )

        # Create links for foreign keys
        node_ids = set(node["id"] for node in nodes)
        for obj in db_objects:
            for fk in obj["foreign_keys"]:
                # Only include links if both source and target are in our nodes
                if (
                    obj["schema"] + "." + obj["name"] in node_ids
                    and fk["referenced_table"] in node_ids
                ):
                    links.append(
                        {
                            "source": f"{obj['schema']}.{obj['name']}",
                            "target": fk["referenced_table"],
                            "sourceColumn": fk["column"],
                            "targetColumn": fk["referenced_column"],
                        }
                    )

        return jsonify({"nodes": nodes, "links": links})
    except Exception as e:
        return jsonify({"error": str(e), "nodes": [], "links": []})


@app.route("/search")
def search():
    """Global search across all databases"""
    q = request.args.get("q", "").lower()
    show_system = request.args.get("show_system", "").lower() == "true"

    if not q or len(q) < 3:
        return render_template(
            "search.html",
            query=q,
            results=[],
            error="Please enter at least 3 characters",
            show_system=show_system,
        )

    results = []
    databases = list_databases()

    for db in databases:
        try:
            conn = get_connection(db)
            cur = conn.cursor()

            # Filter system schemas if not showing system objects
            schema_filter = ""
            schema_params = []
            if not show_system:
                placeholders = ",".join(["?"] * len(SYSTEM_SCHEMAS))
                schema_filter = f"AND TABLE_SCHEMA NOT IN ({placeholders})"
                schema_params = list(SYSTEM_SCHEMAS)

            # Search tables
            params = [f"%{q}%", f"%{q}%"] + schema_params
            cur.execute(
                f"""
                SELECT 'Table' as obj_type, TABLE_SCHEMA, TABLE_NAME
                FROM INFORMATION_SCHEMA.TABLES
                WHERE TABLE_TYPE='BASE TABLE'
                    AND (TABLE_NAME LIKE ? OR TABLE_SCHEMA LIKE ?)
                    {schema_filter}
                """,
                params,
            )

            for row in cur.fetchall():
                results.append(
                    {
                        "database": db,
                        "obj_type": row.obj_type,
                        "schema": row.TABLE_SCHEMA,
                        "name": row.TABLE_NAME,
                        "is_system": is_system_schema(row.TABLE_SCHEMA),
                    }
                )

            # Search views
            cur.execute(
                f"""
                SELECT 'View' as obj_type, TABLE_SCHEMA, TABLE_NAME
                FROM INFORMATION_SCHEMA.VIEWS
                WHERE (TABLE_NAME LIKE ? OR TABLE_SCHEMA LIKE ?)
                {schema_filter}
                """,
                params,
            )

            for row in cur.fetchall():
                results.append(
                    {
                        "database": db,
                        "obj_type": row.obj_type,
                        "schema": row.TABLE_SCHEMA,
                        "name": row.TABLE_NAME,
                        "is_system": is_system_schema(row.TABLE_SCHEMA),
                    }
                )

            # Search columns
            column_params = [f"%{q}%"] + schema_params
            cur.execute(
                f"""
                SELECT 'Column' as obj_type, TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE COLUMN_NAME LIKE ?
                {schema_filter}
                """,
                column_params,
            )

            for row in cur.fetchall():
                results.append(
                    {
                        "database": db,
                        "obj_type": row.obj_type,
                        "schema": row.TABLE_SCHEMA,
                        "name": row.TABLE_NAME,
                        "column": row.COLUMN_NAME,
                        "is_system": is_system_schema(row.TABLE_SCHEMA),
                    }
                )

            conn.close()
        except Exception as e:
            print(f"Error searching {db}: {e}")

    # Filter out system objects if needed
    if not show_system:
        results = [r for r in results if not r["is_system"]]

    return render_template(
        "search.html", query=q, results=results, show_system=show_system
    )


if __name__ == "__main__":
    app.run(debug=True)
