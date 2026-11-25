import asyncio
import json
import logging
import os
from unittest.mock import MagicMock, patch
from cbpi.api import CBPiActor, Property, parameters, action

logger = logging.getLogger(__name__)

# БЕЗОПАСНАЯ ЗАГЛУШКА ДЛЯ WINDOWS
try:
    import RPi.GPIO as GPIO
except Exception:
    logger.warning("Failed to load RPi.GPIO. Using Mock instead (Windows test mode)")
    MockRPi = MagicMock()
    modules = {"RPi": MockRPi, "RPi.GPIO": MockRPi.GPIO}
    patcher = patch.dict("sys.modules", modules)
    patcher.start()
    import RPi.GPIO as GPIO

# Отключаем бесполезные предупреждения о занятости пинов
GPIO.setwarnings(False)

# Проверка и установка режима BCM
if GPIO.getmode() is None:
    GPIO.setmode(GPIO.BCM)

try:
    from pymodbus.client import ModbusSerialClient
except ImportError:
    logger.error("pymodbus not installed. Run: pipx runpip cbpi4 install pymodbus pyserial")
    ModbusSerialClient = None


# ---------------------------------------------------------------------------
# Хранение последней заданной пользователем мощности.
# Файл лежит в папке конфигурации CBPi4 (рядом с actor.json) и переживает
# перезапуск CBPi4 и перезагрузку Raspberry Pi.
# Пока файла/записи для этого актора нет (самый первый запуск) - мощность 0%.
# ---------------------------------------------------------------------------
POWER_FILE_NAME = "gpiomodbusactor_power.json"
FIRST_START_POWER = 0   # мощность при самом первом запуске, %


def _clamp_power(value):
    """Приводит значение к целому числу 0..100."""
    try:
        value = int(float(value))
    except (TypeError, ValueError):
        return FIRST_START_POWER
    return max(0, min(100, value))


@parameters([
    Property.Select(
        label="GPIO",
        options=[str(i) for i in range(2, 28)], # 2 to 27 (исключены пины 0 и 1 - ID_SD/ID_SC)
        description="GPIO pin (BCM) for the Run/Stop relay (Direct dry contact)"
    ),
    Property.Text(label="Serial Port", configurable=True, default_value="/dev/ttyUSB0", description="Serial port (e.g., /dev/ttyUSB0)"),
    Property.Number(label="Baudrate", configurable=True, default_value=4800, description="Baudrate (4800, 9600, 19200, etc.)"),
    Property.Number(label="Slave ID", configurable=True, default_value=1, description="Modbus Slave ID (1-254)"),
    Property.Number(label="Register Address", configurable=True, default_value=0, description="Register address for 0-10V output"),
    Property.Number(label="Max Value", configurable=True, default_value=4095, description="Max value for 100% power (e.g., 4095)")
])
class GPIOModbusActor(CBPiActor):

    def __init__(self, cbpi, id, props):
        super(GPIOModbusActor, self).__init__(cbpi, id, props)
        self.client = None
        self.power = 0
        self.state = False
        self.gpio = None  # Защитная инициализация
        self.serial_port = None
        self.baudrate = 4800
        self.slave_id = 1
        self.register = 0
        self.max_value = 4095

    # ----- сохранение / загрузка мощности -----------------------------------
    def _power_file(self):
        cf = getattr(self.cbpi, "config_folder", None)
        try:
            return cf.get_file_path(POWER_FILE_NAME)   # папка config CBPi4
        except Exception:
            return os.path.join("./config", POWER_FILE_NAME)

    def _load_saved_power(self):
        """Возвращает сохранённую мощность или FIRST_START_POWER, если её ещё нет."""
        try:
            with open(self._power_file(), "r", encoding="utf-8") as f:
                data = json.load(f)
            if str(self.id) in data:
                return _clamp_power(data[str(self.id)])
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning(f"GPIOModbusActor {self.id}: cannot read saved power: {e}")
        return FIRST_START_POWER

    def _save_power(self):
        """Записывает текущую уставку мощности в файл (атомарно)."""
        path = self._power_file()
        try:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
            data[str(self.id)] = self.power
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, path)
        except Exception as e:
            logger.warning(f"GPIOModbusActor {self.id}: cannot save power: {e}")

    async def on_start(self):
        """Инициализация GPIO и Modbus"""
        # Восстанавливаем мощность: при первом запуске 0%, дальше - последнее значение пользователя
        self.power = self._load_saved_power()
        try:
            await self.cbpi.actor.actor_update(self.id, self.power)
        except Exception:
            pass
        try:
            self.gpio = int(self.props.get("GPIO", 17))
            
            GPIO.setup(self.gpio, GPIO.OUT)
            GPIO.output(self.gpio, GPIO.LOW)
            
            self.serial_port = self.props.get("Serial Port", "/dev/ttyUSB0")
            self.baudrate = int(self.props.get("Baudrate", 4800))
            self.slave_id = int(self.props.get("Slave ID", 1))
            self.register = int(self.props.get("Register Address", 0))
            self.max_value = int(self.props.get("Max Value", 4095))

            if ModbusSerialClient is None:
                logger.error("Cannot initialize Modbus: pymodbus library is missing")
                return

            await asyncio.sleep(0.1)

            self.client = ModbusSerialClient(
                port=self.serial_port, baudrate=self.baudrate, bytesize=8, parity='N', stopbits=1, timeout=1
            )
            
            if self.client.connect():
                logger.info(f"GPIOModbusActor {self.id} connected to {self.serial_port}")
            else:
                logger.warning(f"GPIOModbusActor {self.id} could not connect to {self.serial_port}")
                
        except Exception as e:
            logger.error(f"Error initializing GPIOModbusActor {self.id}: {e}")
            self.client = None

    async def on(self, power=None, output=None):
        """ВКЛЮЧЕНИЕ: задаём сохранённую скорость и замыкаем прямой контакт.

        Переданное значение power игнорируется: CBPi4 при пустом значении
        подставляет 100%, а нам нужна мощность, заданная пользователем
        (при первом запуске - 0%). Изменить её можно ползунком / окном Set Power.
        """
        self.power = _clamp_power(self.power)

        # 1. Отправляем уставку скорости и проверяем результат
        success = await self._write_modbus_power(self.power)

        # 2. Замыкаем контакт ТОЛЬКО если Modbus ответил успешно (Fail-Safe)
        if success:
            GPIO.output(self.gpio, GPIO.HIGH)
            logger.info(f"GPIOModbusActor {self.id} ON - GPIO {self.gpio} (HIGH) - Power {self.power}%")
            self.state = True
            await self.cbpi.actor.actor_update(self.id, self.power)
        else:
            logger.warning(f"GPIOModbusActor {self.id}: Modbus write FAILED. GPIO NOT closed for safety!")

    async def off(self):
        """ВЫКЛЮЧЕНИЕ: Размыкаем контакт и сбрасываем скорость"""
        logger.info(f"GPIOModbusActor {self.id} OFF - GPIO {self.gpio} (LOW)")
        
        # 1. Размыкаем прямой сухой контакт (Штатный стоп)
        if self.gpio is not None:
            GPIO.output(self.gpio, GPIO.LOW)
        
        # 2. Сбрасываем скорость в 0 (Страховка на случай залипания реле)
        await self._write_modbus_power(0)
        
        self.state = False
        # Уставку пользователя не сбрасываем: показываем её в интерфейсе и
        # используем при следующем включении (на выходе Modbus уже 0).
        await self.cbpi.actor.actor_update(self.id, self.power)

    async def set_power(self, power):
        """Обновление мощности БЕЗ изменения состояния ВКЛ/ВЫКЛ (вызывается ползунком в UI)"""
        self.power = _clamp_power(power)
        self._save_power()   # запоминаем значение пользователя

        # Если актор включён - сразу отправляем уставку по Modbus (GPIO не трогаем).
        # Если выключен - только запоминаем: на выходе остаётся 0, значение
        # применится при следующем включении.
        if self.state:
            await self._write_modbus_power(self.power)
        await self.cbpi.actor.actor_update(self.id, self.power)
        logger.info(f"GPIOModbusActor {self.id} power updated to {self.power}% (State unchanged)")

    async def _write_modbus_power(self, power_percent) -> bool:
        """Внутренний метод для отправки значения в Modbus. Возвращает True при успехе."""
        # ИСПРАВЛЕНО: проверяем и отсутствие клиента, и отвалившееся соединение
        if self.client is None or not self.client.connected:
            try:
                if self.client:
                    self.client.close()
                self.client = ModbusSerialClient(
                    port=self.serial_port,
                    baudrate=self.baudrate,
                    bytesize=8,
                    parity='N',
                    stopbits=1,
                    timeout=1
                )
                self.client.connect()
            except Exception as e:
                logger.warning(f"Modbus reconnection failed: {e}")
                return False

        if self.client and self.client.connected:
            register_value = int((power_percent / 100.0) * self.max_value)
            try:
                response = self.client.write_register(self.register, register_value, device_id=self.slave_id)
                if response is None or response.isError():
                    logger.warning(f"Modbus write failed or returned error for actor {self.id}")
                    return False
                return True
            except Exception as e:
                logger.warning(f"Modbus write exception: {e}")
                return False
        return False

    @action(key="Set Power", parameters=[Property.Number(label="Power", configurable=True, description="Power Setting [0-100]")])
    async def setpower(self, Power=0, **kwargs):
        """Окно 'Set Power' в интерфейсе CBPi4"""
        await self.set_power(Power)

    def get_state(self):
        return self.state

    async def run(self):
        while self.running:
            await asyncio.sleep(1)

    async def on_stop(self):
        """Аварийная остановка"""
        logger.info(f"GPIOModbusActor {self.id} on_stop triggered")
        
        if self.gpio is not None:
            GPIO.output(self.gpio, GPIO.LOW)
        
        # Пытаемся записать 0 всегда, даже если клиент не подключен (попытка переподключения)
        try:
            await self._write_modbus_power(0)
            logger.info(f"GPIOModbusActor {self.id}: Modbus forced to 0 on stop")
        except Exception as e:
            logger.warning(f"Failed to write 0 on stop: {e}")
        
        if self.client:
            try:
                self.client.close()
            except Exception:
                pass


def setup(cbpi):
    cbpi.plugin.register("GPIOModbusActor", GPIOModbusActor)
    logger.info("GPIOModbusActor plugin loaded")
