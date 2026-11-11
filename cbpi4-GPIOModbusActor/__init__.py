import asyncio
import logging
import RPi.GPIO as GPIO
from cbpi.api import CBPiActor, Property, parameters

logger = logging.getLogger(__name__)

try:
    from pymodbus.client import ModbusSerialClient
except ImportError:
    logger.error("pymodbus not installed. Run: pipx runpip cbpi4 install pymodbus pyserial")
    ModbusSerialClient = None

@parameters([
    Property.Text(label="Serial Port", configurable=True, default_value="/dev/ttyUSB0", description="Serial port (e.g., /dev/ttyUSB0)"),
    Property.Number(label="Baudrate", configurable=True, default_value=4800, description="Baudrate (4800, 9600, 19200, etc.)"),
    Property.Number(label="Slave ID", configurable=True, default_value=1, description="Modbus Slave ID (1-254)"),
    Property.Number(label="Register Address", configurable=True, default_value=0, description="Register address for 0-10V output"),
    Property.Number(label="Max Value", configurable=True, default_value=4095, description="Max value for 100% power (e.g., 4095)"),
    Property.Number(label="Run GPIO Pin", configurable=True, default_value=17, description="GPIO pin (BCM) for the Run/Stop relay")
])
class GPIOModbusActor(CBPiActor):

    def __init__(self, cbpi, id, props):
        super(GPIOModbusActor, self).__init__(cbpi, id, props)
        self.client = None
        self.power = 0
        self.state = False
        
        # Настройка GPIO
        self.run_pin = int(self.props.get("Run GPIO Pin", 17))
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(self.run_pin, GPIO.OUT)
        GPIO.output(self.run_pin, GPIO.LOW) # По умолчанию реле разомкнуто (Стоп)
        logger.info(f"GPIOModbusActor {self.id} initialized. Run relay on GPIO {self.run_pin}")

    async def on_start(self):
        """Инициализация Modbus"""
        try:
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
        """ВКЛЮЧЕНИЕ: Замыкаем реле И отправляем скорость"""
        # 1. Замыкаем сухой контакт (Пуск)
        GPIO.output(self.run_pin, GPIO.HIGH)
        logger.info(f"GPIOModbusActor {self.id}: Run relay CLOSED (GPIO {self.run_pin})")
        
        # 2. Отправляем уставку скорости
        if power is not None:
            await self.set_power(power)
        else:
            await self.set_power(100)
            
        self.state = True

    async def off(self):
        """ВЫКЛЮЧЕНИЕ: Размыкаем реле И сбрасываем скорость в 0"""
        # 1. Отправляем 0 скорости (плавный стоп частотника)
        await self.set_power(0)
        
        # 2. Размыкаем сухой контакт (Стоп)
        GPIO.output(self.run_pin, GPIO.LOW)
        logger.info(f"GPIOModbusActor {self.id}: Run relay OPEN (GPIO {self.run_pin})")
        
        self.state = False

    async def set_power(self, power):
        """Отправка значения 0-10V через Modbus"""
        self.power = int(power)
        if self.power < 0: self.power = 0
        if self.power > 100: self.power = 100

        if self.client is None or not self.client.connected:
            try:
                if self.client: self.client.close()
                self.client = ModbusSerialClient(
                    port=self.serial_port, baudrate=self.baudrate, bytesize=8, parity='N', stopbits=1, timeout=1
                )
                self.client.connect()
            except Exception as e:
                logger.warning(f"Reconnection failed: {e}")

        if self.client and self.client.connected:
            register_value = int((self.power / 100.0) * self.max_value)
            await self._write_register(register_value)
        
        await self.cbpi.actor.actor_update(self.id, self.power)

    async def _write_register(self, value):
        """Запись в регистр с проверкой"""
        try:
            response = self.client.write_register(self.register, value, device_id=self.slave_id)
            if response is None:
                logger.warning(f"Modbus write returned None for actor {self.id}")
            elif response.isError():
                logger.warning(f"Modbus write error for actor {self.id}")
            else:
                logger.info(f"GPIOModbusActor {self.id}: Wrote {value} to register {self.register}")
        except Exception as e:
            logger.warning(f"Modbus write exception: {e}")

    def get_state(self):
        return self.state

    async def run(self):
        while self.running:
            await asyncio.sleep(1)

    async def on_stop(self):
        """Безопасная остановка: сброс в 0 и размыкание реле"""
        if self.client and self.client.connected:
            try:
                await self._write_register(0)
                logger.info(f"GPIOModbusActor {self.id}: Output forced to 0 on stop")
            except Exception as e:
                logger.warning(f"Failed to write 0 on stop: {e}")
        
        # Гарантированно размыкаем реле при любом завершении
        GPIO.output(self.run_pin, GPIO.LOW)
        logger.info(f"GPIOModbusActor {self.id}: Run relay forced OPEN on stop")
        
        if self.client:
            try:
                self.client.close()
            except Exception:
                pass


def setup(cbpi):
    cbpi.plugin.register("GPIOModbusActor", GPIOModbusActor)
    logger.info("GPIOModbusActor plugin loaded")