#include <gpio.h>

void digitalWrite(GPIO_TypeDef* GPIOx , uint8_t pin , uint8_t value){

  if (value == HIGH){
   GPIOx -> BSRR = (1U << 13);
  }
  else {
    GPIOx -> BSRR = (1U << (pin + 16));
  }
  
}

void init_gpio_c (){
 
  RCC->APB2ENR |= (1U << 4);
   
}

void pinMode(GPIO_TypeDef* GPIOx , uint8_t pin , uint8_t mode){
 
  uint8_t bit_shift = (pin % 8) * 4;

  uint32_t config_val = (mode == OUTPUT) ? 0x2U : 0X4U;

  if (pin < 8)
  {
    GPIOx -> CRL &= ~(0xFU << bit_shift);

    GPIOx -> CRL |= (config_val << bit_shift);
  }
  else {
   GPIOx -> CRH &= ~(0XFU << bit_shift);

   GPIOx -> CRH |= (config_val << bit_shift); 
  }
}