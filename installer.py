import os
import json
import textwrap

project_structure = {
    "run.py": """
import asyncio
import logging
import sys
import os
import signal
from hybrid.server import HybridServer

def setup_logging():
    log_dir = "logs"
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)
    log_format = "[%(asctime)s] [%(name)s/%(levelname)s]: %(message)s"
    date_format = "%H:%M:%S"
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter(log_format, date_format))
    root_logger.addHandler(console_handler)
    file_handler = logging.FileHandler(os.path.join(log_dir, "hybrid.log"), mode='w', encoding='utf-8')
    file_handler.setFormatter(logging.Formatter(log_format, date_format))
    root_logger.addHandler(file_handler)

def main():
    setup_logging()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    server = HybridServer()
    main_loop = asyncio.get_event_loop()
    try:
        if sys.platform != "win32":
            main_loop.add_signal_handler(signal.SIGINT, lambda: asyncio.create_task(server.stop()))
            main_loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.create_task(server.stop()))
    except NotImplementedError:
        pass
    try:
        main_loop.run_until_complete(server.run())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        if server.is_running():
            main_loop.run_until_complete(server.stop())
        logging.getLogger("Hybrid").info("Servidor Hybrid desligado.")

if __name__ == "__main__":
    main()
""",
    "hybrid/__init__.py": "",
    "hybrid/server.py": """
import logging
import asyncio
from .process_manager import ProcessManager
from .plugins.plugin_manager import PluginManager
from .addons.addon_manager import AddonManager
from .event_manager import EventManager

class HybridServer:
    def __init__(self):
        self.logger = logging.getLogger("HybridServer")
        self._state = "stopped"
        self.event_manager = EventManager()
        self.plugin_manager = PluginManager(self)
        self.addon_manager = AddonManager(self)
        self.process_manager = ProcessManager(self)
        self._main_task = None

    @property
    def state(self):
        return self._state

    @state.setter
    def state(self, new_state):
        if self._state != new_state:
            self.logger.info(f"Estado do servidor alterado: {self._state} -> {new_state}")
            self._state = new_state

    async def run(self):
        if self.state != "stopped":
            self.logger.warning("Tentativa de iniciar um servidor que não está parado.")
            return
        self.state = "starting"
        self.logger.info("Iniciando Hybrid Server...")
        self.addon_manager.load_and_inject_addons()
        self.plugin_manager.load_plugins()
        self.plugin_manager.enable_plugins()
        self._main_task = asyncio.create_task(self.process_manager.start_and_monitor_bds())
        try:
            await self._main_task
        except asyncio.CancelledError:
            self.logger.info("Tarefa principal do servidor cancelada.")

    async def stop(self):
        if self.state in ["stopping", "stopped"]:
            return
        self.state = "stopping"
        self.logger.info("Desabilitando plugins...")
        self.plugin_manager.disable_plugins()
        self.logger.info("Parando o Bedrock Dedicated Server...")
        await self.process_manager.stop_bds()
        if self._main_task and not self._main_task.done():
            self._main_task.cancel()
        self.state = "stopped"

    def is_running(self):
        return self.state == "running"
""",
    "hybrid/process_manager.py": """
import asyncio
import logging
import os
import sys
from .api.events import ServerLogEvent, PlayerJoinEvent

class ProcessManager:
    def __init__(self, server_instance):
        self.server = server_instance
        self.event_manager = self.server.event_manager
        self.logger = logging.getLogger("ProcessManager")
        self.process = None
        self.bds_path = "./bedrock_server" if sys.platform != "win32" else "./bedrock_server.exe"

    async def start_and_monitor_bds(self):
        if not os.path.exists(self.bds_path):
            self.logger.critical(f"Executável do BDS não encontrado em '{self.bds_path}'!")
            self.logger.critical("Por favor, baixe o Bedrock Dedicated Server e coloque o executável na pasta raiz.")
            self.server.state = "crashed"
            return
        self.logger.info(f"Iniciando processo: {self.bds_path}")
        env = os.environ.copy()
        if sys.platform != "win32":
            env["LD_LIBRARY_PATH"] = "."
        try:
            self.process = await asyncio.create_subprocess_exec(
                self.bds_path,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env
            )
        except Exception as e:
            self.logger.critical(f"Falha ao iniciar o processo do BDS: {e}")
            self.server.state = "crashed"
            return
        self.server.state = "running"
        output_reader_task = asyncio.create_task(self._read_output())
        await self.process.wait()
        output_reader_task.cancel()
        if self.server.state == "stopping":
             self.logger.info(f"Processo do BDS encerrado de forma limpa (código: {self.process.returncode}).")
             self.server.state = "stopped"
        else:
            self.logger.error(f"Processo do BDS encerrou inesperadamente (código: {self.process.returncode}).")
            self.server.state = "crashed"

    async def _read_output(self):
        try:
            while True:
                line_bytes = await self.process.stdout.readline()
                if not line_bytes:
                    break
                line = line_bytes.decode('utf-8', errors='ignore').strip()
                if line:
                    await self.event_manager.dispatch(ServerLogEvent(line))
                    if "Player connected:" in line:
                        try:
                            parts = line.split("Player connected: ")[1].split(',')
                            player_name = parts[0].strip()
                            await self.event_manager.dispatch(PlayerJoinEvent(player_name))
                        except IndexError:
                            self.logger.warning(f"Não foi possível extrair o nome do jogador da linha: {line}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.logger.error(f"Erro ao ler a saída do BDS: {e}")

    async def stop_bds(self):
        if self.process and self.process.returncode is None:
            self.logger.info("Enviando comando 'stop' para o BDS...")
            try:
                self.process.stdin.write(b"stop\\n")
                await self.process.stdin.drain()
                await asyncio.wait_for(self.process.wait(), timeout=30.0)
            except asyncio.TimeoutError:
                self.logger.warning("O processo do BDS não encerrou em 30s. Forçando o encerramento.")
                self.process.kill()
            except (BrokenPipeError, ConnectionResetError):
                self.logger.warning("A comunicação com o BDS já estava fechada ao tentar parar.")
""",
    "hybrid/event_manager.py": """
import asyncio
import logging
from collections import defaultdict

class EventManager:
    def __init__(self):
        self.listeners = defaultdict(list)

    def register_listener(self, plugin_instance, event_class, handler_func):
        self.listeners[event_class].append(handler_func)
        logging.debug(f"Plugin '{plugin_instance.__class__.__module__}' registrou um listener para '{event_class.__name__}'.")

    async def dispatch(self, event):
        event_class = type(event)
        if event_class in self.listeners:
            tasks = [
                asyncio.create_task(handler(event)) 
                for handler in self.listeners[event_class]
            ]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
""",
    "hybrid/api/__init__.py": "",
    "hybrid/api/plugin.py": """
import logging

class HybridPlugin:
    def __init__(self, server_instance, manifest_data):
        self.server = server_instance
        self.manifest = manifest_data
        self.name = manifest_data.get("name", "UnnamedPlugin")
        self.logger = logging.getLogger(f"Plugin.{self.name}")

    def on_enable(self):
        pass

    def on_disable(self):
        pass
""",
    "hybrid/api/events.py": """
class Event:
    pass

class ServerLogEvent(Event):
    def __init__(self, line):
        self.line = line

class PlayerJoinEvent(Event):
    def __init__(self, player_name):
        self.player_name = player_name
""",
    "hybrid/plugins/__init__.py": "",
    "hybrid/plugins/plugin_manager.py": """
import os
import json
import importlib.util
import logging
from ..api.plugin import HybridPlugin

class PluginManager:
    def __init__(self, server_instance):
        self.server = server_instance
        self.logger = logging.getLogger("PluginManager")
        self.plugins_dir = "plugins"
        self.loaded_plugins = {}

    def load_plugins(self):
        self.logger.info(f"Procurando por plugins em '{self.plugins_dir}/'...")
        if not os.path.isdir(self.plugins_dir):
            self.logger.warning("Diretório de plugins não encontrado. Criando...")
            os.makedirs(self.plugins_dir)
            return
        for plugin_name in os.listdir(self.plugins_dir):
            plugin_path = os.path.join(self.plugins_dir, plugin_name)
            if os.path.isdir(plugin_path):
                self._load_plugin_from_dir(plugin_name, plugin_path)

    def _load_plugin_from_dir(self, plugin_name, plugin_path):
        manifest_path = os.path.join(plugin_path, "plugin.json")
        if not os.path.exists(manifest_path):
            return
        try:
            with open(manifest_path, 'r', encoding='utf-8') as f:
                manifest = json.load(f)
            required_keys = ["name", "main", "class", "version"]
            if not all(key in manifest for key in required_keys):
                self.logger.error(f"Plugin '{plugin_name}' ignorado: 'plugin.json' incompleto.")
                return
            main_file = manifest["main"]
            main_path = os.path.join(plugin_path, main_file)
            if not os.path.exists(main_path):
                self.logger.error(f"Plugin '{plugin_name}' ignorado: Arquivo principal '{main_file}' não encontrado.")
                return
            spec = importlib.util.spec_from_file_location(f"hybrid.plugins.user.{plugin_name}", main_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            plugin_class = getattr(module, manifest["class"], None)
            if plugin_class and issubclass(plugin_class, HybridPlugin):
                plugin_instance = plugin_class(self.server, manifest)
                self.loaded_plugins[manifest['name']] = plugin_instance
                self.logger.info(f"Plugin '{manifest['name']}' v{manifest['version']} carregado.")
            else:
                self.logger.error(f"Classe principal '{manifest['class']}' não encontrada ou inválida no plugin '{plugin_name}'.")
        except Exception as e:
            self.logger.error(f"Falha crítica ao carregar o plugin '{plugin_name}': {e}", exc_info=True)

    def enable_plugins(self):
        if not self.loaded_plugins:
            self.logger.info("Nenhum plugin para habilitar.")
            return
        self.logger.info("Habilitando plugins...")
        for name, plugin in self.loaded_plugins.items():
            try:
                plugin.on_enable()
            except Exception as e:
                self.logger.error(f"Erro ao habilitar o plugin '{name}': {e}", exc_info=True)

    def disable_plugins(self):
        if not self.loaded_plugins:
            return
        self.logger.info("Desabilitando plugins...")
        for name, plugin in self.loaded_plugins.items():
            try:
                plugin.on_disable()
            except Exception as e:
                self.logger.error(f"Erro ao desabilitar o plugin '{name}': {e}", exc_info=True)
""",
    "hybrid/addons/__init__.py": "",
    "hybrid/addons/addon_manager.py": """
import os
import json
import logging

class AddonManager:
    def __init__(self, server_instance):
        self.server = server_instance
        self.logger = logging.getLogger("AddonManager")
        self.behavior_packs_dir = "behavior_packs"
        self.resource_packs_dir = "resource_packs"
        self.loaded_behavior_packs = []
        self.loaded_resource_packs = []

    def _get_world_name(self):
        try:
            with open("server.properties", 'r') as f:
                for line in f:
                    if line.startswith("level-name="):
                        return line.strip().split("=")[1]
        except FileNotFoundError:
            self.logger.warning("'server.properties' não encontrado. Usando nome de mundo padrão 'Bedrock level'.")
            return "Bedrock level"
        return "Bedrock level"

    def _scan_packs(self, pack_dir):
        found_packs = []
        if not os.path.isdir(pack_dir):
            self.logger.warning(f"Diretório '{pack_dir}' não encontrado. Criando...")
            os.makedirs(pack_dir)
            return found_packs
        for pack_name in os.listdir(pack_dir):
            pack_path = os.path.join(pack_dir, pack_name)
            manifest_path = os.path.join(pack_path, "manifest.json")
            if os.path.isdir(pack_path) and os.path.exists(manifest_path):
                try:
                    with open(manifest_path, 'r', encoding='utf-8') as f:
                        manifest = json.load(f)
                    header = manifest.get("header", {})
                    pack_uuid = header.get("uuid")
                    pack_version = header.get("version")
                    if not pack_uuid or not pack_version:
                        self.logger.error(f"Add-on '{pack_name}' ignorado: UUID ou versão ausente no manifest.json.")
                        continue
                    found_packs.append({"pack_id": pack_uuid, "version": pack_version})
                    self.logger.info(f"Add-on '{header.get('name', pack_name)}' v{pack_version} detectado.")
                except Exception as e:
                    self.logger.error(f"Falha ao ler o manifest do add-on '{pack_name}': {e}")
        return found_packs

    def _write_world_pack_file(self, world_path, filename, packs_data):
        file_path = os.path.join(world_path, filename)
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(packs_data, f, indent=4)
            self.logger.info(f"Arquivo '{filename}' atualizado com {len(packs_data)} pack(s).")
        except Exception as e:
            self.logger.error(f"Não foi possível escrever em '{file_path}': {e}")

    def load_and_inject_addons(self):
        self.logger.info("Iniciando detecção e injeção de Add-ons...")
        world_name = self._get_world_name()
        world_path = os.path.join("worlds", world_name)
        if not os.path.isdir(world_path):
            self.logger.warning(f"Pasta do mundo '{world_path}' não encontrada. O BDS irá criá-la.")
            self.logger.warning("A injeção de Add-ons só funcionará na segunda inicialização do servidor.")
            return
        self.loaded_behavior_packs = self._scan_packs(self.behavior_packs_dir)
        self.loaded_resource_packs = self._scan_packs(self.resource_packs_dir)
        if self.loaded_behavior_packs:
            self._write_world_pack_file(world_path, "world_behavior_packs.json", self.loaded_behavior_packs)
        if self.loaded_resource_packs:
            self._write_world_pack_file(world_path, "world_resource_packs.json", self.loaded_resource_packs)
        self.logger.info("Processo de Add-ons concluído.")
""",
    "plugins/welcome_message/plugin.json": """
{
    "name": "WelcomeMessage",
    "main": "main.py",
    "class": "WelcomePlugin",
    "version": "1.0",
    "api-version": "1.0",
    "author": "Hybrid Team"
}
""",
    "plugins/welcome_message/main.py": """
from hybrid.api.plugin import HybridPlugin
from hybrid.api.events import PlayerJoinEvent

class WelcomePlugin(HybridPlugin):
    def on_enable(self):
        self.logger.info("Plugin de Boas-Vindas habilitado!")
        self.server.event_manager.register_listener(self, PlayerJoinEvent, self.on_player_join)

    async def on_player_join(self, event: PlayerJoinEvent):
        player_name = event.player_name
        self.logger.info(f"O jogador {player_name} entrou! Enviando mensagem de boas-vindas.")
        command = f"say Bem-vindo ao servidor Hybrid, {player_name}!\\n"
        stdin = self.server.process_manager.process.stdin
        if stdin and not stdin.is_closing():
            stdin.write(command.encode())
            await stdin.drain()

    def on_disable(self):
        self.logger.info("Plugin de Boas-Vindas desabilitado.")
"""
}

for file_path, content in project_structure.items():
    dir_name = os.path.dirname(file_path)
    if dir_name and not os.path.exists(dir_name):
        os.makedirs(dir_name)
    
    with open(file_path, 'w', encoding='utf-8') as f:
        f.write(textwrap.dedent(content).strip())

