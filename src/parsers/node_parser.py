"""Node.js / TypeScript backend parser — Express, NestJS, AWS Lambda, CDK."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any


class NodeParser:
    def parse(self, local_path: str) -> dict[str, Any]:
        root = Path(local_path)
        result: dict[str, Any] = {
            "service_name": root.name,
            "tech_stack": ["Node.js"],
            "apis": [],
            "databases": [],
            "downstream_calls": [],
            "dependencies": [],
            "topics": [],
            "environments": [],
            "tables": [],
            "modules": [],
            "cloud_resources": [],
            "pipelines": [],
            "features": [],
            "description_hints": "",
        }

        self._parse_package_json(root, result)
        self._parse_env_files(root, result)
        self._parse_source_files(root, result)
        self._parse_serverless_config(root, result)
        self._parse_cdk(root, result)

        result["tech_stack"] = list(set(result["tech_stack"]))
        result["description_hints"] = (
            f"Node.js service '{result['service_name']}' "
            f"built with {', '.join(result['tech_stack'][:4])}. "
            f"Exposes {len(result['apis'])} endpoint(s), "
            f"calls {len(result['downstream_calls'])} downstream service(s)."
        )
        return result

    # ── package.json ──────────────────────────────────────────────────────────
    def _parse_package_json(self, root: Path, result: dict) -> None:
        p = root / "package.json"
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
            if "name" in data:
                result["service_name"] = data["name"]
            all_deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
            for pkg, ver in all_deps.items():
                result["dependencies"].append(f"{pkg}@{ver}")
                pkg_l = pkg.lower()
                if pkg_l in ("express", "fastify", "koa", "hapi"):
                    result["tech_stack"].append(pkg.capitalize())
                if "@nestjs/core" == pkg_l or "@nestjs/common" == pkg_l:
                    result["tech_stack"].append("NestJS")
                if pkg_l == "typescript":
                    result["tech_stack"].append("TypeScript")
                if pkg_l in ("mongoose", "@types/mongoose"):
                    result["tech_stack"].append("MongoDB")
                if pkg_l in ("pg", "pg-pool", "postgres"):
                    result["tech_stack"].append("PostgreSQL")
                if pkg_l in ("mysql", "mysql2"):
                    result["tech_stack"].append("MySQL")
                if pkg_l in ("redis", "ioredis"):
                    result["tech_stack"].append("Redis")
                if "kafka" in pkg_l or "kafkajs" == pkg_l:
                    result["tech_stack"].append("Kafka")
                if pkg_l in ("aws-sdk", "@aws-sdk/client-lambda"):
                    result["tech_stack"].append("AWS SDK")
                if "aws-cdk" in pkg_l or pkg_l == "aws-cdk-lib":
                    result["tech_stack"].append("AWS CDK")
                if pkg_l in ("sequelize", "typeorm", "prisma", "@prisma/client", "knex"):
                    result["tech_stack"].append("ORM")
                if pkg_l == "graphql":
                    result["tech_stack"].append("GraphQL")
                if pkg_l in ("socket.io", "ws"):
                    result["tech_stack"].append("WebSocket")
        except Exception:
            pass

    # ── .env files ────────────────────────────────────────────────────────────
    def _parse_env_files(self, root: Path, result: dict) -> None:
        for env_file in list(root.glob(".env")) + list(root.glob(".env.*")):
            env_name = re.match(r"\.env\.(.+)", env_file.name)
            if env_name and env_name.group(1) not in result["environments"]:
                result["environments"].append(env_name.group(1).lower())
            try:
                content = env_file.read_text(encoding="utf-8", errors="ignore")
                for line in content.splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    key, _, val = line.partition("=")
                    key = key.strip()
                    val = val.strip().strip('"').strip("'")
                    key_u = key.upper()
                    if any(k in key_u for k in ("DATABASE_URL", "DB_URL", "MONGO_URI",
                                                  "POSTGRES_URL", "MYSQL_URL", "REDIS_URL")):
                        result["databases"].append({"url": val, "source": env_file.name})
                        if "mongo" in val.lower():
                            result["tech_stack"].append("MongoDB")
                        if "postgres" in val.lower():
                            result["tech_stack"].append("PostgreSQL")
                        if "mysql" in val.lower():
                            result["tech_stack"].append("MySQL")
                        if "redis" in val.lower():
                            result["tech_stack"].append("Redis")
                    elif any(k in key_u for k in ("API_URL", "SERVICE_URL", "BASE_URL",
                                                    "ENDPOINT_URL", "HOST_URL")):
                        if val.startswith("http") and val not in result["downstream_calls"]:
                            result["downstream_calls"].append(val)
                    elif "PORT" == key_u:
                        result["tech_stack"].append(f"HTTP:{val}")
            except Exception:
                pass

    # ── TypeScript / JavaScript source files ──────────────────────────────────
    def _parse_source_files(self, root: Path, result: dict) -> None:
        skip = {"node_modules", ".git", "dist", "build", "coverage", ".serverless",
                "cdk.out", "__pycache__", ".next"}
        for dirpath, dirs, files in os.walk(str(root)):
            dirs[:] = [d for d in dirs if d not in skip]
            for fname in files:
                if not any(fname.endswith(ext) for ext in (".ts", ".js", ".mjs", ".cjs")):
                    continue
                if fname.endswith(".d.ts") or fname.endswith(".test.ts") or fname.endswith(".spec.ts"):
                    continue
                fpath = Path(dirpath) / fname
                try:
                    content = fpath.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                rel = str(fpath.relative_to(root))

                # Express routes: app.get('/path', ...) or router.post(...)
                for method, path in re.findall(
                    r"""(?:app|router|server)\s*\.\s*(get|post|put|patch|delete|all)\s*\(\s*['"`]([^'"`]+)['"`]""",
                    content, re.IGNORECASE
                ):
                    result["apis"].append({
                        "path": path, "method": method.upper(), "source": rel
                    })

                # NestJS @Controller + @Get/@Post etc.
                if "@Controller" in content or "@Get(" in content or "@Post(" in content:
                    ctrl_base = re.search(r"@Controller\s*\(\s*['\"`]([^'\"`]*)['\"`]", content)
                    base = ctrl_base.group(1) if ctrl_base else ""
                    for ann, path in re.findall(
                        r"@(Get|Post|Put|Delete|Patch|All)\s*\(\s*['\"`]([^'\"`]*)['\"`]", content
                    ):
                        full = ("/" + base.lstrip("/") + "/" + path.lstrip("/")).rstrip("/") or "/"
                        result["apis"].append({"path": full, "method": ann.upper(), "source": rel})
                    # NestJS module name
                    mod = re.search(r"@Module\s*\(", content)
                    if mod:
                        class_name = re.search(r"class\s+(\w+)", content)
                        if class_name and class_name.group(1) not in result["modules"]:
                            result["modules"].append(class_name.group(1))

                # AWS Lambda handler exports
                if "exports.handler" in content or "export const handler" in content:
                    result["tech_stack"].append("AWS Lambda")
                    # event source: APIGateway, SQS, SNS, Kinesis
                    if "event.Records" in content and "body" in content:
                        result["tech_stack"].append("API Gateway")
                    if "Records[0].kinesis" in content:
                        result["tech_stack"].append("Kinesis")
                    if "Records[0].Sns" in content:
                        result["tech_stack"].append("SNS")

                # HTTP calls (axios, fetch, got, node-fetch)
                for method, url in re.findall(
                    r"axios\.(get|post|put|delete|patch)\s*\(\s*['\"`]([^'\"`]+)['\"`]",
                    content
                ):
                    if url not in result["downstream_calls"]:
                        result["downstream_calls"].append(url)
                for url in re.findall(r"fetch\s*\(\s*['\"`]([^'\"`]+)['\"`]", content):
                    if url.startswith("http") and url not in result["downstream_calls"]:
                        result["downstream_calls"].append(url)
                for url in re.findall(r"(?:got|request)\s*\(\s*['\"`](https?://[^'\"`]+)['\"`]", content):
                    if url not in result["downstream_calls"]:
                        result["downstream_calls"].append(url)

                # DB connections
                for url in re.findall(
                    r"""(?:createConnection|connect|MongoClient|mongoose\.connect|createPool)\s*\(\s*['\"`]([^'\"`]+)['\"`]""",
                    content
                ):
                    if any(k in url.lower() for k in ("mongo", "postgres", "mysql", "redis",
                                                        "localhost", "rds", "db")):
                        result["databases"].append({"url": url, "source": rel})

                # Mongoose schemas → table names
                for model in re.findall(r"new\s+Schema\s*\(|model\s*\(\s*['\"`](\w+)['\"`]", content):
                    if model and model not in result["tables"]:
                        result["tables"].append(model)

                # Kafka topics
                for topic in re.findall(
                    r"""(?:subscribe|send|produce|consumer\.subscribe)\s*\(\s*\{[^}]*topic[s]?\s*:\s*['\"`]([^'\"`]+)['\"`]""",
                    content
                ):
                    if topic not in result["topics"]:
                        result["topics"].append(topic)

                # SQS queue names
                for queue in re.findall(r"""QueueUrl\s*:\s*['\"`]([^'\"`]+)['\"`]""", content):
                    if queue not in result["topics"]:
                        result["topics"].append(queue)

    # ── serverless.yml / serverless.ts ────────────────────────────────────────
    def _parse_serverless_config(self, root: Path, result: dict) -> None:
        for sls in ["serverless.yml", "serverless.yaml", "serverless.ts"]:
            p = root / sls
            if not p.exists():
                continue
            try:
                content = p.read_text(encoding="utf-8", errors="ignore")
                result["tech_stack"].append("Serverless Framework")
                result["tech_stack"].append("AWS Lambda")
                # functions
                for fn_name in re.findall(r"^  (\w+):\s*$", content, re.MULTILINE):
                    if fn_name not in ("plugins", "custom", "provider", "package", "resources"):
                        result["modules"].append(fn_name)
                # http events → API endpoints
                for method, path in re.findall(
                    r"method:\s*(\w+)\s*\n\s*path:\s*([^\n]+)", content
                ):
                    result["apis"].append({"path": path.strip(), "method": method.upper(),
                                           "source": sls})
                for path, method in re.findall(
                    r"path:\s*([^\n]+)\s*\n\s*method:\s*(\w+)", content
                ):
                    result["apis"].append({"path": path.strip(), "method": method.upper(),
                                           "source": sls})
                # stage/environment
                for stage in re.findall(r"stage:\s*(\w+)", content):
                    if stage not in result["environments"]:
                        result["environments"].append(stage)
                # region
                for region in re.findall(r"region:\s*([\w-]+)", content):
                    result["cloud_resources"].append({"type": "aws_region", "name": region})
            except Exception:
                pass

    # ── AWS CDK ───────────────────────────────────────────────────────────────
    def _parse_cdk(self, root: Path, result: dict) -> None:
        cdk_out = root / "cdk.out"
        if cdk_out.exists():
            result["tech_stack"].append("AWS CDK")

        skip = {"node_modules", ".git", "dist", "cdk.out"}
        for dirpath, dirs, files in os.walk(str(root)):
            dirs[:] = [d for d in dirs if d not in skip]
            for fname in files:
                if not fname.endswith(".ts"):
                    continue
                fpath = Path(dirpath) / fname
                try:
                    content = fpath.read_text(encoding="utf-8", errors="ignore")
                    if "aws-cdk-lib" not in content and "aws-cdk" not in content:
                        continue
                    # Lambda functions defined in CDK
                    for fn in re.findall(r"new\s+lambda\.Function\s*\(\s*\w+\s*,\s*['\"`]([^'\"`]+)['\"`]",
                                         content):
                        result["cloud_resources"].append({"type": "aws_lambda_function", "name": fn})
                    # DynamoDB tables in CDK
                    for tbl in re.findall(r"new\s+dynamodb\.Table\s*\(\s*\w+\s*,\s*['\"`]([^'\"`]+)['\"`]",
                                          content):
                        result["cloud_resources"].append({"type": "aws_dynamodb_table", "name": tbl})
                    # RDS / databases in CDK
                    for db in re.findall(r"new\s+rds\.\w+\s*\(\s*\w+\s*,\s*['\"`]([^'\"`]+)['\"`]",
                                         content):
                        result["databases"].append({"name": db, "source": fname})
                    # API Gateway
                    if "apigateway" in content.lower() or "RestApi" in content:
                        result["tech_stack"].append("API Gateway")
                    # SQS
                    if "new sqs.Queue" in content:
                        result["tech_stack"].append("SQS")
                    # SNS
                    if "new sns.Topic" in content:
                        result["tech_stack"].append("SNS")
                    # ECS
                    if "new ecs." in content:
                        result["tech_stack"].append("ECS")
                except Exception:
                    pass
