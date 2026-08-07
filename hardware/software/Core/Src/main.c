#include "stm32f1xx.h"
#include <delay.h>
#include <gpio.h>

int main(){

  init_gpio_c();

  delay_init();

  pinMode(GPIOC , 13 , OUTPUT);


while (1)
{
 digitalWrite(GPIOC , 13 , HIGH);
 delay(500);
 digitalWrite(GPIOC , 13 , LOW);
 delay(200);
}
}