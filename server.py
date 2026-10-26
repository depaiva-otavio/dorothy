import json
import logging
import sqlite3
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parent
DATABASE = ROOT / "db" / "pesquisa.db"
HOST = "127.0.0.1"
PORT = 8000
MAX_REQUEST_BYTES = 16_384
AGE_GROUPS = (
    "7 a 8 anos",
    "9 a 11 anos",
    "12 a 14 anos",
    "15 a 17 anos",
    "18 ou mais",
)
QUESTIONS_PER_ROUND = 4
QUESTION_COUNTS_BY_AGE = {
    "7 a 8 anos": 100,
    "9 a 11 anos": 20,
    "12 a 14 anos": 20,
    "15 a 17 anos": 20,
    "18 ou mais": 20,
}
ROUNDS_BEFORE_REPEAT = 100


def initialize_database():
    DATABASE.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DATABASE) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS surveys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                school TEXT NOT NULL,
                grade TEXT NOT NULL,
                idade TEXT NOT NULL CHECK (idade IN ('7 a 8 anos', '9 a 11 anos', '12 a 14 anos', '15 a 17 anos', '18 ou mais')),
                liked_game TEXT NOT NULL CHECK (liked_game IN ('S', 'N')),
                expo_xp_rating INTEGER NOT NULL CHECK (expo_xp_rating BETWEEN 0 AND 10),
                pontos INTEGER NOT NULL DEFAULT 0 CHECK (pontos BETWEEN 0 AND 999),
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            )
            """
        )
        survey_columns = {
            column[1]
            for column in connection.execute("PRAGMA table_info(surveys)").fetchall()
        }
        if "pontos" not in survey_columns:
            connection.execute(
                "ALTER TABLE surveys ADD COLUMN pontos INTEGER NOT NULL DEFAULT 0 "
                "CHECK (pontos BETWEEN 0 AND 999)"
            )
        if "idade" not in survey_columns:
            connection.execute(
                "ALTER TABLE surveys ADD COLUMN idade TEXT NOT NULL DEFAULT '18 ou mais' "
                "CHECK (idade IN ('7 a 8 anos', '9 a 11 anos', '12 a 14 anos', '15 a 17 anos', '18 ou mais'))"
            )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS game_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                player_id INTEGER NOT NULL REFERENCES surveys(id),
                points INTEGER NOT NULL CHECK (points BETWEEN 0 AND 4),
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS question_rounds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                player_id INTEGER NOT NULL REFERENCES surveys(id) ON DELETE CASCADE,
                round_token TEXT NOT NULL,
                idade TEXT NOT NULL CHECK (idade IN ('7 a 8 anos', '9 a 11 anos', '12 a 14 anos', '15 a 17 anos', '18 ou mais')),
                question_ids TEXT NOT NULL,
                question_key TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                UNIQUE (player_id, round_token)
            )
            """
        )


class GameRequestHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self):
        if urlsplit(self.path).path == "/index.html":
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self):
        path = unquote(urlsplit(self.path).path)
        if path == "/api/health":
            self._send_json(200, {"status": "ok"})
            return

        if path == "/api/registration-count":
            try:
                registration_count = self._get_registration_count()
            except sqlite3.Error:
                logging.exception("Falha ao consultar o total de inscrições.")
                self._send_json(500, {"error": "Não foi possível consultar o total de inscrições."})
                return
            self._send_json(200, {"count": registration_count})
            return

        if path.startswith("/api/player-ranking/"):
            player_id_text = path.removeprefix("/api/player-ranking/")
            if not player_id_text.isdecimal():
                self._send_json(400, {"error": "Identificador de jogador inválido."})
                return
            try:
                ranking = self._get_player_ranking(int(player_id_text))
            except sqlite3.Error:
                logging.exception("Falha ao consultar a classificação do jogador.")
                self._send_json(500, {"error": "Não foi possível consultar a classificação."})
                return
            if ranking is None:
                self._send_json(404, {"error": "Jogador não encontrado."})
                return
            self._send_json(200, ranking)
            return

        if path in ("", "/"):
            self.path = "/index.html"
        elif path.startswith("/img/"):
            requested_file = (ROOT / path.lstrip("/")).resolve()
            image_directory = (ROOT / "img").resolve()
            if requested_file.parent != image_directory or requested_file.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
                self.send_error(404)
                return
        elif path != "/index.html":
            self.send_error(404)
            return

        super().do_GET()

    def do_OPTIONS(self):
        if urlsplit(self.path).path not in ("/api/survey", "/api/game-results", "/api/question-round"):
            self.send_error(404)
            return

        origin = self.headers.get("Origin")
        if not self._is_local_origin(origin):
            self._send_json(403, {"error": "A origem da página não é permitida."})
            return

        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Vary", "Origin")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        path = urlsplit(self.path).path
        if path == "/api/question-round":
            self._handle_question_round()
            return
        if path == "/api/game-results":
            self._handle_game_result()
            return
        if path != "/api/survey":
            self.send_error(404)
            return

        origin = self.headers.get("Origin")
        if origin and not self._is_local_origin(origin):
            self._send_json(403, {"error": "A origem da página não é permitida."})
            return

        content_type = self.headers.get_content_type()
        if content_type != "application/json":
            self._send_json(415, {"error": "O formulário deve ser enviado em JSON."})
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, {"error": "Tamanho da solicitação inválido."})
            return

        if content_length <= 0 or content_length > MAX_REQUEST_BYTES:
            self._send_json(413, {"error": "Tamanho da solicitação inválido."})
            return

        try:
            payload = json.loads(self.rfile.read(content_length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(400, {"error": "O formulário contém JSON inválido."})
            return

        if not isinstance(payload, dict):
            self._send_json(400, {"error": "Os dados do formulário são inválidos."})
            return

        text_fields = ("name", "school", "grade")
        values = {}
        for field in text_fields:
            value = payload.get(field)
            if not isinstance(value, str) or not value.strip() or len(value.strip()) > 120:
                self._send_json(400, {"error": "Preencha nome, escola e série (até 120 caracteres)."})
                return
            values[field] = value.strip()

        age = payload.get("idade")
        if age not in AGE_GROUPS:
            self._send_json(400, {"error": "Selecione uma faixa etária válida."})
            return

        liked_game = payload.get("liked_game")
        if liked_game not in ("S", "N"):
            self._send_json(400, {"error": "Informe se gostou do jogo."})
            return

        rating = payload.get("expo_xp_rating")
        if isinstance(rating, bool) or not isinstance(rating, int) or not 0 <= rating <= 10:
            self._send_json(400, {"error": "A nota Expo XP deve ser um número de 0 a 10."})
            return

        try:
            with sqlite3.connect(DATABASE) as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO surveys (name, school, grade, idade, liked_game, expo_xp_rating)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        values["name"],
                        values["school"],
                        values["grade"],
                        age,
                        liked_game,
                        rating,
                    ),
                )
                registration_count = connection.execute(
                    "SELECT COUNT(*) FROM surveys"
                ).fetchone()[0]
        except sqlite3.Error:
            logging.exception("Falha ao salvar resposta da pesquisa no SQLite.")
            self._send_json(500, {"error": "Não foi possível salvar a pesquisa. Tente novamente."})
            return

        self._send_json(
            201,
            {
                "id": cursor.lastrowid,
                "message": "Pesquisa salva com sucesso.",
                "registration_count": registration_count,
            },
        )

    def _handle_question_round(self):
        origin = self.headers.get("Origin")
        if origin and not self._is_local_origin(origin):
            self._send_json(403, {"error": "A origem da página não é permitida."})
            return

        if self.headers.get_content_type() != "application/json":
            self._send_json(415, {"error": "A rodada deve ser enviada em JSON."})
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, {"error": "Tamanho da solicitação inválido."})
            return

        if content_length <= 0 or content_length > MAX_REQUEST_BYTES:
            self._send_json(413, {"error": "Tamanho da solicitação inválido."})
            return

        try:
            payload = json.loads(self.rfile.read(content_length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(400, {"error": "A rodada contém JSON inválido."})
            return

        if not isinstance(payload, dict):
            self._send_json(400, {"error": "Os dados da rodada são inválidos."})
            return

        player_id = payload.get("player_id")
        if isinstance(player_id, bool) or not isinstance(player_id, int) or player_id <= 0:
            self._send_json(400, {"error": "Identificador de jogador inválido."})
            return

        round_token = payload.get("round_token")
        if not isinstance(round_token, str) or not round_token.strip() or len(round_token) > 100:
            self._send_json(400, {"error": "Identificador da partida inválido."})
            return

        question_ids = payload.get("question_ids")
        if (
            not isinstance(question_ids, list)
            or len(question_ids) != QUESTIONS_PER_ROUND
            or any(
                isinstance(question_id, bool)
                or not isinstance(question_id, int)
                or not 0 <= question_id < max(QUESTION_COUNTS_BY_AGE.values())
                for question_id in question_ids
            )
            or len(set(question_ids)) != QUESTIONS_PER_ROUND
        ):
            self._send_json(400, {"error": "A rodada deve conter quatro perguntas diferentes."})
            return

        question_key = json.dumps(sorted(question_ids), separators=(",", ":"))
        try:
            with sqlite3.connect(DATABASE) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("BEGIN IMMEDIATE")
                player = connection.execute(
                    "SELECT idade FROM surveys WHERE id = ?",
                    (player_id,),
                ).fetchone()
                if player is None:
                    self._send_json(404, {"error": "Jogador não encontrado."})
                    return

                if any(
                    question_id >= QUESTION_COUNTS_BY_AGE[player["idade"]]
                    for question_id in question_ids
                ):
                    self._send_json(400, {"error": "A rodada contém perguntas fora da faixa etária do jogador."})
                    return

                existing_round = connection.execute(
                    "SELECT question_ids FROM question_rounds WHERE player_id = ? AND round_token = ?",
                    (player_id, round_token),
                ).fetchone()
                if existing_round:
                    assigned_question_ids = json.loads(existing_round["question_ids"])
                else:
                    age_round_count = connection.execute(
                        "SELECT COUNT(*) FROM question_rounds WHERE idade = ?",
                        (player["idade"],),
                    ).fetchone()[0]
                    if age_round_count < ROUNDS_BEFORE_REPEAT:
                        duplicate_round = connection.execute(
                            "SELECT 1 FROM question_rounds WHERE idade = ? AND question_key = ?",
                            (player["idade"], question_key),
                        ).fetchone()
                        if duplicate_round:
                            self._send_json(409, {"error": "Esta combinação já foi usada nessa faixa etária."})
                            return

                    assigned_question_ids = question_ids
                    connection.execute(
                        """
                        INSERT INTO question_rounds (player_id, round_token, idade, question_ids, question_key)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            player_id,
                            round_token,
                            player["idade"],
                            json.dumps(assigned_question_ids),
                            question_key,
                        ),
                    )
                    age_round_count += 1
                age_round_count = connection.execute(
                    "SELECT COUNT(*) FROM question_rounds WHERE idade = ?",
                    (player["idade"],),
                ).fetchone()[0]
        except sqlite3.Error:
            logging.exception("Falha ao reservar a rodada de perguntas no SQLite.")
            self._send_json(500, {"error": "Não foi possível reservar as perguntas. Tente novamente."})
            return

        self._send_json(
            200,
            {
                "idade": player["idade"],
                "question_ids": assigned_question_ids,
                "players_in_age_group": age_round_count,
                "players_before_repeat": ROUNDS_BEFORE_REPEAT,
            },
        )

    def _handle_game_result(self):
        origin = self.headers.get("Origin")
        if origin and not self._is_local_origin(origin):
            self._send_json(403, {"error": "A origem da página não é permitida."})
            return

        if self.headers.get_content_type() != "application/json":
            self._send_json(415, {"error": "O resultado deve ser enviado em JSON."})
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, {"error": "Tamanho da solicitação inválido."})
            return

        if content_length <= 0 or content_length > MAX_REQUEST_BYTES:
            self._send_json(413, {"error": "Tamanho da solicitação inválido."})
            return

        try:
            payload = json.loads(self.rfile.read(content_length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(400, {"error": "O resultado contém JSON inválido."})
            return

        if not isinstance(payload, dict):
            self._send_json(400, {"error": "Os dados do resultado são inválidos."})
            return

        player_id = payload.get("player_id")
        if isinstance(player_id, bool) or not isinstance(player_id, int) or player_id <= 0:
            self._send_json(400, {"error": "Identificador de jogador inválido."})
            return

        points = payload.get("points")
        if isinstance(points, bool) or not isinstance(points, int) or not 0 <= points <= 4:
            self._send_json(400, {"error": "A pontuação deve estar entre 0 e 4."})
            return

        limit_exceeded = False
        player_not_found = False
        try:
            with sqlite3.connect(DATABASE) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("BEGIN IMMEDIATE")
                player = connection.execute(
                    "SELECT id, name, school FROM surveys WHERE id = ?",
                    (player_id,),
                ).fetchone()
                if player is None:
                    player_not_found = True
                else:
                    player_points = connection.execute(
                        """
                        SELECT COALESCE(SUM(pontos), 0)
                        FROM surveys
                        WHERE lower(trim(name)) = lower(trim(?))
                          AND lower(trim(school)) = lower(trim(?))
                        """,
                        (player["name"], player["school"]),
                    ).fetchone()[0]
                    updated_points = player_points + points
                    if updated_points > 999:
                        limit_exceeded = True
                    else:
                        canonical_player_id = connection.execute(
                            """
                            SELECT MIN(id)
                            FROM surveys
                            WHERE lower(trim(name)) = lower(trim(?))
                              AND lower(trim(school)) = lower(trim(?))
                            """,
                            (player["name"], player["school"]),
                        ).fetchone()[0]
                        connection.execute(
                            """
                            UPDATE surveys
                            SET pontos = CASE WHEN id = ? THEN ? ELSE 0 END
                            WHERE lower(trim(name)) = lower(trim(?))
                              AND lower(trim(school)) = lower(trim(?))
                            """,
                            (
                                canonical_player_id,
                                updated_points,
                                player["name"],
                                player["school"],
                            ),
                        )
                        cursor = connection.execute(
                            "INSERT INTO game_results (player_id, points) VALUES (?, ?)",
                            (player_id, points),
                        )
                        result_id = cursor.lastrowid
                        player_id = canonical_player_id
        except sqlite3.Error:
            logging.exception("Falha ao salvar o resultado da partida no SQLite.")
            self._send_json(500, {"error": "Não foi possível salvar a pontuação. Tente novamente."})
            return

        if player_not_found:
            self._send_json(404, {"error": "Jogador não encontrado."})
            return
        if limit_exceeded:
            self._send_json(409, {"error": "O limite de 999 pontos foi atingido. Esta partida não foi contabilizada."})
            return

        try:
            ranking = self._get_player_ranking(player_id)
        except sqlite3.Error:
            logging.exception("Falha ao consultar a classificação após salvar a pontuação.")
            self._send_json(500, {"error": "A pontuação foi salva, mas não foi possível consultar a classificação."})
            return

        self._send_json(201, {"id": result_id, "ranking": ranking})

    @staticmethod
    def _get_registration_count():
        with sqlite3.connect(DATABASE) as connection:
            return connection.execute("SELECT COUNT(*) FROM surveys").fetchone()[0]

    @staticmethod
    def _get_player_ranking(player_id):
        with sqlite3.connect(DATABASE) as connection:
            connection.row_factory = sqlite3.Row
            player = connection.execute(
                "SELECT id, name, school FROM surveys WHERE id = ?",
                (player_id,),
            ).fetchone()
            if player is None:
                return None

            stats = connection.execute(
                """
                SELECT
                    COALESCE((
                        SELECT SUM(related_player.pontos)
                        FROM surveys AS related_player
                        WHERE lower(trim(related_player.name)) = lower(trim(?))
                          AND lower(trim(related_player.school)) = lower(trim(?))
                    ), 0) AS points,
                    (
                        SELECT COUNT(result.id)
                        FROM game_results AS result
                        JOIN surveys AS related_player ON related_player.id = result.player_id
                        WHERE lower(trim(related_player.name)) = lower(trim(?))
                          AND lower(trim(related_player.school)) = lower(trim(?))
                    ) AS games
                """,
                (
                    player["name"],
                    player["school"],
                    player["name"],
                    player["school"],
                ),
            ).fetchone()
            ranking = connection.execute(
                """
                WITH player_totals AS (
                    SELECT
                        lower(trim(player.name)) AS normalized_name,
                        lower(trim(player.school)) AS normalized_school,
                        SUM(player.pontos) AS total_points
                    FROM surveys AS player
                    GROUP BY normalized_name, normalized_school
                    HAVING SUM(player.pontos) > 0
                ),
                ranked_players AS (
                    SELECT
                        normalized_name,
                        normalized_school,
                        RANK() OVER (ORDER BY total_points DESC) AS position
                    FROM player_totals
                )
                SELECT position
                FROM ranked_players
                WHERE normalized_name = lower(trim(?))
                  AND normalized_school = lower(trim(?))
                """,
                (player["name"], player["school"]),
            ).fetchone()

        return {
            "name": player["name"],
            "school": player["school"],
            "games": stats["games"],
            "points": stats["points"],
            "position": ranking["position"] if ranking else None,
        }

    @staticmethod
    def _is_local_origin(origin):
        if not origin:
            return False

        try:
            parsed_origin = urlsplit(origin)
            return (
                parsed_origin.scheme == "http"
                and parsed_origin.hostname in ("localhost", "127.0.0.1")
                and (parsed_origin.port is None or 1 <= parsed_origin.port <= 65535)
                and not parsed_origin.username
                and not parsed_origin.password
                and not parsed_origin.path
                and not parsed_origin.query
                and not parsed_origin.fragment
            )
        except ValueError:
            return False

    def _send_json(self, status, payload):
        response = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(response)))
        self.send_header("Cache-Control", "no-store")
        origin = self.headers.get("Origin")
        if self._is_local_origin(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(response)


def main():
    initialize_database()
    server = ThreadingHTTPServer((HOST, PORT), GameRequestHandler)
    print(f"Jogo disponível em http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor encerrado.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
