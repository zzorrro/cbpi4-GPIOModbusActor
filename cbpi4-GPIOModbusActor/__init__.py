import asyncio
import logging
from unittest.mock import MagicMock, patch
from cbpi.api import CBPiActor, Property, parameters

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

    async def on_start(self):
        """Инициализация GPIO и Modbus"""
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

    async def on(self, power=None):
        """ВКЛЮЧЕНИЕ: Задаем скорость и замыкаем прямой контакт"""
        # Если мощность передана явно — используем её
        if power is not None:
            self.power = int(float(power))
        # Если мощность не передана — используем ранее заданную уставку
        # Защита от первого запуска с нулевой уставкой
        if self.power <= 0:
            self.power = 100
            
        if self.power > 100: self.power = 100

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
        await self.cbpi.actor.actor_update(self.id, 0)

    async def set_power(self, power):
        """Обновление мощности БЕЗ изменения состояния ВКЛ/ВЫКЛ (вызывается ползунком в UI)"""
        self.power = int(float(power))
        if self.power < 0: self.power = 0
        if self.power > 100: self.power = 100
        
        # Просто отправляем новую уставку по Modbus, не трогая GPIO
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
