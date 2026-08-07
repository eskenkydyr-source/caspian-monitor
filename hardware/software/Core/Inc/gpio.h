#ifndef GPIO_H
#define GPIO_H

#include "stm32f1xx.h"

#define LOW 0
#define HIGH 1
#define OUTPUT 1
#define INPUT 0

void digitalWrite(GPIO_TypeDef* GPIOx , uint8_t pin , uint8_t value);
void init_gpio_c ();
void pinMode(GPIO_TypeDef* GPIOx , uint8_t pin , uint8_t mode);

#endif