from setuptools import setup, find_packages

setup(
    name='cbpi4-GPIOModbusActor',
    version='1.0.0',
    description='CBPi4 Actor for VFD control via GPIO (Run/Stop) and RS485 Modbus RTU (0-10V speed)',
    author='Sammy Brewery',
    url='https://github.com/zzorrro/cbpi4-GPIOModbusActor',
    license='MIT',
    packages=find_packages(),
    install_requires=[
        'pymodbus>=3.0.0',
        'pyserial>=3.5',
        'RPi.GPIO>=0.7.0'
    ]
)