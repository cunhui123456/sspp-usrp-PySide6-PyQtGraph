#include <Wire.h>

template<class T> inline Print &operator <<(Print &obj, T arg) {
  obj.print(arg);
  return obj;
}

#define FILTER_WINDOW_SIZE 10
#define Vref 5

int sensorValue[FILTER_WINDOW_SIZE];
float VoltValue = 0;

// TCA9555 I2C 地址（默认0x20，A0/A1/A2可调整）
#define TCA9555_ADDR 0x20
// 寄存器地址（参考数据手册）
#define REG_INPUT_PORT0 0x00
#define REG_INPUT_PORT1 0x01
#define REG_OUTPUT_PORT0 0x02
#define REG_OUTPUT_PORT1 0x03
#define REG_CONFIG_PORT0 0x06
#define REG_CONFIG_PORT1 0x07

int Pin = 2;//0-4，0-6，传感器编号，0-4是压阻，5，6是温度
//int Pin = 6;//0-4，0-6，传感器编号，0-4是压阻，5，6是温度
void setup() {
  // put your setup code here, to run once:
  Serial.begin(9600);
  Wire.begin(); // 初始化I2C
  
  // 配置所有引脚为输出（0=输出，1=输入）
  writeTCA9555(REG_CONFIG_PORT0, 0x00); // P0-P7 设为输出
  writeTCA9555(REG_CONFIG_PORT1, 0x00); // P8-P15 设为输出

  // 设置某个引脚为1，其他为0（示例：P3=1，其余=0）
  uint16_t pinMask = (1 << Pin);  // 选择P3（0-15对应P0-P15），00000000 00000010
  setPinHighOnly(pinMask);

}

void loop() {
  // put your main code here, to run repeatedly:
  float runningSum= 0;
  //读100个数据取平均值
  for(int i=0; i<FILTER_WINDOW_SIZE; i++) { 
      sensorValue[i] = analogRead(A2);//A1是温度，A2是压力
      runningSum = runningSum + float(sensorValue[i]* 5) / 1023; 
  }
  VoltValue = runningSum / FILTER_WINDOW_SIZE;
  Serial.print(millis());
  Serial.print(",");
  Serial.println(VoltValue);
  // Serial << "传感器D" << Pin+1 << "的电压为: " << VoltValue  << "\n";
  delay(50);
}

// 设置指定引脚为高电平，其他为低电平
void setPinHighOnly(uint16_t pinMask) {
  // 计算PORT0（P0-P7）和PORT1（P8-P15）的值
  uint8_t port0Value = (pinMask & 0xFF);      // 低8位（P0-P7）
  uint8_t port1Value = (pinMask >> 8) & 0xFF; // 高8位（P8-P15）

  // 写入输出寄存器
  writeTCA9555(REG_OUTPUT_PORT0, port0Value); // 取反（0=低电平，1=高电平）
  writeTCA9555(REG_OUTPUT_PORT1, port1Value);
}

// 向TCA9555写入数据
void writeTCA9555(uint8_t reg, uint8_t value) {
  Wire.beginTransmission(TCA9555_ADDR);
  Wire.write(reg);    // 指定寄存器
  Wire.write(value);  // 写入值
  Wire.endTransmission();
}