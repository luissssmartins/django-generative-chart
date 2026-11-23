import ast
import re
import warnings
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from django.db import connections
from django.db.utils import load_backend
from django.views.generic import TemplateView


class DiagramaView(TemplateView):
    template_name = "diagrama/diagrama.html"
    pastas_ignoradas = [
        ".git",
        ".venv",
        "venv",
        "env",
        "node_modules",
        "site-packages",
        "migrations",
        "__pycache__",
    ]
    campos_relacao = ["ForeignKey", "OneToOneField", "ManyToManyField"]
    metodos_escrita = [
        "create",
        "save",
        "delete",
        "update",
        "bulk_create",
        "bulk_update",
        "get_or_create",
        "update_or_create",
    ]
    views_escrita = ["Create", "Update", "Delete", "Destroy", "Form", "ModelViewSet"]
    bancos = {
        "postgres": "postgresql",
        "postgresql": "postgresql",
        "mysql": "mysql",
        "oracle": "oracle",
    }

    def post(self, request):
        caminho = request.POST.get("caminho", "").strip()
        url_banco = request.POST.get("banco", "").strip()
        secoes = []
        erro = None

        try:
            if caminho:
                secoes += self.get_projeto(caminho)
            if url_banco:
                secoes.append(self.get_banco(url_banco))
        except Exception as excecao:
            erro = str(excecao)

        codigos = [secao["codigo"] for secao in secoes]
        context = self.get_context_data(
            caminho=caminho, secoes=secoes, erro=erro, codigos=codigos
        )
        return self.render_to_response(context)

    @staticmethod
    def nome_formatado(elemento):
        if isinstance(elemento, ast.Name):
            return elemento.id
        if isinstance(elemento, ast.Attribute):
            return elemento.attr
        if isinstance(elemento, ast.Constant) and isinstance(elemento.value, str):
            return elemento.value.split(".")[-1]
        return None

    def get_projeto(self, caminho):
        raiz = Path(caminho).expanduser().resolve()
        if not raiz.is_dir():
            raise ValueError(f"Pasta não encontrada: {raiz}")

        arquivos = []
        for pasta, subpastas, nomes_arquivos in raiz.walk():
            subpastas[:] = sorted(
                subpasta
                for subpasta in subpastas
                if subpasta not in self.pastas_ignoradas
            )
            for nome_arquivo in sorted(nomes_arquivos):
                if not nome_arquivo.endswith(".py"):
                    continue
                arquivo = pasta / nome_arquivo
                try:
                    with warnings.catch_warnings(action="ignore"):
                        arvore = ast.parse(arquivo.read_text(encoding="utf-8"))
                except (SyntaxError, UnicodeDecodeError, ValueError):
                    continue
                modulo = ".".join(arquivo.relative_to(raiz).with_suffix("").parts)
                arquivos.append((arquivo, modulo, arvore))

        classes = []
        for _, _, arvore in arquivos:
            classes += [
                classe
                for classe in ast.walk(arvore)
                if isinstance(classe, ast.ClassDef)
            ]

        modelos = {}
        found = True
        while found:
            found = False
            for classe in classes:
                bases = [self.nome_formatado(base) for base in classe.bases]
                if classe.name not in modelos and (
                    "Model" in bases or any(base in modelos for base in bases)
                ):
                    modelos[classe.name] = classe
                    found = True

        classes_com_model = {}
        for classe in classes:
            for meta in classe.body:
                if not isinstance(meta, ast.ClassDef) or meta.name != "Meta":
                    continue
                for atributo in meta.body:
                    if (
                        isinstance(atributo, ast.Assign)
                        and self.nome_formatado(atributo.targets[0]) == "model"
                    ):
                        modelo = self.nome_formatado(atributo.value)
                        if modelo in modelos:
                            classes_com_model[classe.name] = modelo

        entidades = {}
        relacoes = []
        for nome_modelo, classe in modelos.items():
            campos = []
            for atributo in classe.body:
                if not isinstance(atributo, ast.Assign) or not isinstance(
                    atributo.value, ast.Call
                ):
                    continue
                if not isinstance(atributo.targets[0], ast.Name):
                    continue

                tipo = self.nome_formatado(atributo.value.func)
                campo = atributo.targets[0].id
                if not tipo or not (
                    tipo.endswith("Field") or tipo in self.campos_relacao
                ):
                    continue

                argumentos = {
                    argumento.arg: argumento.value
                    for argumento in atributo.value.keywords
                }
                primary_key = argumentos.get("primary_key")
                null = argumentos.get("null")
                chave = (
                    "PK"
                    if isinstance(primary_key, ast.Constant)
                    and primary_key.value is True
                    else ""
                )

                if tipo in self.campos_relacao:
                    destino = (
                        atributo.value.args[0]
                        if atributo.value.args
                        else argumentos.get("to")
                    )
                    destino = self.nome_formatado(destino)
                    if destino == "self":
                        destino = nome_modelo
                    if destino:
                        nulo = isinstance(null, ast.Constant) and null.value is True
                        relacoes.append((nome_modelo, campo, tipo, destino, nulo))
                    if not chave and tipo != "ManyToManyField":
                        chave = "FK"

                campos.append((tipo, campo, chave))

            if "PK" not in [chave for _, _, chave in campos]:
                campos.insert(0, ("AutoField", "id", "PK"))
            entidades[nome_modelo] = campos

        views = self.get_views(arquivos, modelos, classes_com_model)
        return [
            self.__diagrama_er__("Models do projeto", entidades, relacoes),
            self.__diagrama_views__(views),
        ]

    def get_rotas(self, arquivos):
        urls = {}
        for arquivo, modulo, arvore in arquivos:
            if arquivo.name != "urls.py":
                continue

            itens = []
            for chamada in ast.walk(arvore):
                if not isinstance(chamada, ast.Call) or len(chamada.args) < 2:
                    continue
                funcao = self.nome_formatado(chamada.func)
                if funcao not in ("path", "re_path", "url", "register"):
                    continue

                rota = (
                    chamada.args[0].value.strip("^$")
                    if isinstance(chamada.args[0], ast.Constant)
                    else "?"
                )
                destino = chamada.args[1]

                if funcao == "register":
                    itens.append((rota + "/", "view", self.nome_formatado(destino)))
                elif (
                    isinstance(destino, ast.Call)
                    and self.nome_formatado(destino.func) == "include"
                ):
                    incluido = destino.args[0] if destino.args else None
                    if isinstance(incluido, ast.Tuple):
                        incluido = incluido.elts[0]
                    if isinstance(incluido, ast.Constant):
                        itens.append((rota, "include", incluido.value))
                else:
                    if isinstance(destino, ast.Call) and isinstance(
                        destino.func, ast.Attribute
                    ):
                        destino = destino.func.value
                    itens.append((rota, "view", self.nome_formatado(destino)))

            urls[modulo] = (arquivo, itens)

        incluidos = {}
        for _, itens in urls.values():
            for _, tipo, destino in itens:
                if tipo != "include":
                    continue
                for modulo in urls:
                    if modulo == destino or modulo.endswith("." + destino):
                        incluidos[destino] = modulo
                        break

        rotas = []
        fila = [
            (modulo, "/", [modulo])
            for modulo in urls
            if modulo not in incluidos.values()
        ]
        while fila:
            modulo, prefixo, visitados = fila.pop(0)
            arquivo, itens = urls[modulo]
            for rota, tipo, destino in itens:
                if tipo == "view":
                    rotas.append((prefixo + rota, destino, arquivo.parent))
                elif destino in incluidos and incluidos[destino] not in visitados:
                    proximo = incluidos[destino]
                    fila.append((proximo, prefixo + rota, visitados + [proximo]))
        return rotas

    def get_views(self, arquivos, modelos, classes_com_model):
        definicoes = {}
        for arquivo, modulo, arvore in arquivos:
            for definicao in arvore.body:
                if not isinstance(
                    definicao, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                ):
                    continue
                if definicao.name in modelos or definicao.name in classes_com_model:
                    continue
                definicoes.setdefault(definicao.name, []).append(
                    (arquivo, modulo, definicao)
                )

        encontradas = {}
        for rota, nome_view, pasta in self.get_rotas(arquivos):
            candidatas = definicoes.get(nome_view)
            if not candidatas:
                continue

            escolhida = candidatas[0]
            for candidata in candidatas:
                if candidata[0].is_relative_to(pasta):
                    escolhida = candidata
                    break

            _, modulo, definicao = escolhida
            chave = f"{modulo}.{nome_view}"
            if chave not in encontradas:
                encontradas[chave] = (definicao, [])
            encontradas[chave][1].append(rota)

        for nome_view, candidatas in definicoes.items():
            for _, modulo, definicao in candidatas:
                chave = f"{modulo}.{nome_view}"
                if (
                    "view" in modulo.lower()
                    and not nome_view.startswith("_")
                    and chave not in encontradas
                ):
                    encontradas[chave] = (definicao, [])

        views = []
        for nome_view, (definicao, rotas) in encontradas.items():
            usados = set()
            escritos = set()
            for elemento in ast.walk(definicao):
                if isinstance(elemento, ast.Name):
                    usados.add(classes_com_model.get(elemento.id, elemento.id))
                if isinstance(elemento, ast.Call) and isinstance(
                    elemento.func, ast.Attribute
                ):
                    if elemento.func.attr in self.metodos_escrita:
                        for variavel in ast.walk(elemento.func):
                            if isinstance(variavel, ast.Name):
                                escritos.add(
                                    classes_com_model.get(variavel.id, variavel.id)
                                )

            usados = {modelo for modelo in usados if modelo in modelos}
            if isinstance(definicao, ast.ClassDef):
                bases = [self.nome_formatado(base) or "" for base in definicao.bases]
                if any(termo in base for base in bases for termo in self.views_escrita):
                    escritos = usados

            usos = {}
            for modelo in sorted(usados):
                usos[modelo] = "escrita" if modelo in escritos else "leitura"

            if rotas or usos:
                views.append((nome_view, rotas, usos))
        return views

    def get_banco(self, url):
        usuario = senha = host = porta = ""

        url_parseada = urlparse(url)
        engine = self.bancos.get(url_parseada.scheme.split("+")[0])

        if not engine:
            raise ValueError("Use postgres://, mysql://, oracle://")

        nome_banco = unquote(url_parseada.path[1:])
        usuario = unquote(url_parseada.username or "")
        senha = unquote(url_parseada.password or "")
        host = url_parseada.hostname or ""
        porta = str(url_parseada.port or "")

        # if engine == "sqlite3":
        #     arquivo = Path(nome_banco)
        #     if not arquivo.is_file():
        #         raise ValueError(f"Arquivo SQLite não encontrado: {nome_banco}")
        #     nome_banco = f"file:{quote(str(arquivo.resolve()))}?mode=ro"

        config = connections.configure_settings(
            {
                "default": {
                    "ENGINE": f"django.db.backends.{engine}",
                    "NAME": nome_banco,
                    "USER": usuario,
                    "PASSWORD": senha,
                    "HOST": host,
                    "PORT": porta,
                    "OPTIONS": {"connect_timeout": 5}
                    if engine in ("postgresql", "mysql")
                    else {},
                }
            }
        )["default"]

        conexao = load_backend(config["ENGINE"]).DatabaseWrapper(config, "analise")
        connections["analise"] = conexao
        entidades = {}
        relacoes = []

        try:
            with conexao.cursor() as cursor:
                introspection = conexao.introspection
                for tabela in introspection.get_table_list(cursor):
                    if tabela.type != "t":
                        continue

                    nome_tabela = re.sub(r"\W", "_", tabela.name)
                    pk = introspection.get_primary_key_column(cursor, tabela.name)
                    fks = introspection.get_relations(cursor, tabela.name)
                    colunas = []

                    for coluna in introspection.get_table_description(
                        cursor, tabela.name
                    ):
                        try:
                            tipo = introspection.get_field_type(
                                coluna.type_code, coluna
                            )
                        except KeyError:
                            tipo = str(coluna.type_code)

                        if coluna.name in fks:
                            destino = re.sub(r"\W", "_", fks[coluna.name][1])
                            relacoes.append(
                                (
                                    nome_tabela,
                                    coluna.name,
                                    "ForeignKey",
                                    destino,
                                    coluna.null_ok,
                                )
                            )

                        chave = (
                            "PK"
                            if coluna.name == pk
                            else "FK"
                            if coluna.name in fks
                            else ""
                        )
                        colunas.append(
                            (
                                re.sub(r"\W", "_", tipo),
                                re.sub(r"\W", "_", coluna.name),
                                chave,
                            )
                        )

                    entidades[nome_tabela] = colunas
        finally:
            conexao.close()
            del connections["analise"]

        return self.__diagrama_er__("Banco de dados", entidades, relacoes)

    def __diagrama_er__(self, titulo, entidades, relacoes):
        linhas = ["erDiagram"]
        for entidade, campos in entidades.items():
            linhas.append(f"    {entidade} {{")
            for tipo, campo, chave in campos:
                if campo.lower() in ("pk", "fk", "uk"):
                    campo = f"{campo}_"
                linhas.append(f"        {tipo} {campo} {chave}".rstrip())
            linhas.append("    }")

        tabela = []
        for origem, campo, tipo, destino, nulo in relacoes:
            if tipo == "ManyToManyField":
                linhas.append(f'    {origem} }}o--o{{ {destino} : "{campo}"')
            else:
                inicio = "|o" if nulo else "||"
                fim = "o|" if tipo == "OneToOneField" else "o{"
                linhas.append(f'    {destino} {inicio}--{fim} {origem} : "{campo}"')
            tabela.append((origem, campo, tipo, destino, "sim" if nulo else "não"))

        return {
            "titulo": titulo,
            "codigo": "\n".join(linhas),
            "cabecalho": ["Origem", "Campo", "Tipo", "Destino", "Nulo"],
            "linhas": tabela,
        }

    def __diagrama_views__(self, views):
        linhas = ["flowchart LR"]
        tabela = []
        for posicao_view, (view, rotas, usos) in enumerate(views):
            id_view = f"V{posicao_view}"
            linhas.append(f'    {id_view}["{view}"]')

            for posicao_rota, rota in enumerate(rotas):
                texto = (
                    rota.replace("<", "#lt;")
                    .replace(">", "#gt;")
                    .replace('"', "#quot;")
                )
                linhas.append(
                    f'    {id_view}_R{posicao_rota}(["{texto}"]) --> {id_view}'
                )

            for modelo, operacao in usos.items():
                linhas.append(f"    {id_view} -->|{operacao}| M_{modelo}[({modelo})]")

            rotas_texto = ", ".join(rotas) or "-"
            if not usos:
                tabela.append((rotas_texto, view, "-", "-"))
            for modelo, operacao in usos.items():
                tabela.append((rotas_texto, view, modelo, operacao))

        return {
            "titulo": "Rotas, views e uso dos models",
            "codigo": "\n".join(linhas),
            "cabecalho": ["Rota", "View", "Model", "Operação"],
            "linhas": tabela,
        }
