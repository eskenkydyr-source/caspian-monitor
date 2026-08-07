#include "delay.h"

void delay_init(){
 
    SYSTICK_LOAD = 8000 - 1;

    SYSTICK_VAL = 0;

    SYSTICK_CTRL |= (1 << 0) | (1 << 2);
}

void delay(unsigned int time_ms){

    for(unsigned int i = 0; i < time_ms; i++){

        while ((SYSTICK_CTRL & (1 << 16)) == 0) {
           
    
        }
            (void)SYSTICK_CTRL;
    }
}