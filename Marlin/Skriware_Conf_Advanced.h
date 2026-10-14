#ifndef SK_CONF_ADV
#define SK_CONF_ADV


/*******************************THERMAL PROTECTION *******************************************/

/*
 * Safety review (NORTHBOLT):
 *  - The heat-up check (WATCH_*) is what catches a thermistor that has fallen out of the heater
 *    block: the heater runs flat out while the reading barely moves. 1 C in 45 s let that pass,
 *    so it is back to Marlin's 2 C in 20 s for the hotends and 2 C in 60 s for the bed.
 *  - Runaway hysteresis was 5 C until the 2021 release raised it to 12 C, probably because 5 C
 *    gave false alarms (part fan at full blowing on the nozzle). 8 C is a middle ground. If you
 *    get "Thermal Runaway" stops with the fan at 100 % and a healthy hotend, try 10.
 */
#if ENABLED(THERMAL_PROTECTION_HOTENDS)
  #define THERMAL_PROTECTION_PERIOD 40        // Seconds (was 45)
  #define THERMAL_PROTECTION_HYSTERESIS 8     // Degrees Celsius (was 12)
  #define WATCH_TEMP_PERIOD 20                // Seconds (was 45)
  #define WATCH_TEMP_INCREASE 2               // Degrees Celsius (was 1)
#endif

#if ENABLED(THERMAL_PROTECTION_BED)
  #define THERMAL_PROTECTION_BED_PERIOD 45    // Seconds
  #define THERMAL_PROTECTION_BED_HYSTERESIS 2 // Degrees Celsius
  #define WATCH_BED_TEMP_PERIOD 60                // Seconds (was 90)
  #define WATCH_BED_TEMP_INCREASE 2               // Degrees Celsius (was 1)
#endif

#define AUTOTEMP
#if ENABLED(AUTOTEMP)
  #define AUTOTEMP_OLDWEIGHT 0.98
#endif
/***********************************************************************************************/

/*******************************THEMPERATURE*******************************************/
#define TEMP_SENSOR_AD595_OFFSET 0.0
#define TEMP_SENSOR_AD595_GAIN   1.0

#define E0_AUTO_FAN_PIN -1
#define E1_AUTO_FAN_PIN -1
#define E2_AUTO_FAN_PIN -1
#define E3_AUTO_FAN_PIN -1
#define E4_AUTO_FAN_PIN -1
#define EXTRUDER_AUTO_FAN_TEMPERATURE 50
#define EXTRUDER_AUTO_FAN_SPEED   255  // == full speed
/***********************************************************************************************/

/*******************************HOME*******************************************/
#define X_HOME_BUMP_MM 5
#define Y_HOME_BUMP_MM 5
#define Z_HOME_BUMP_MM 2
#define HOMING_BUMP_DIVISOR {2, 2, 4}  // Re-Bump Speed Divisor (Divides the Homing Feedrate)
#define QUICK_HOME  //if this is defined, if both x and y are to be homed, a diagonal move will be performed initially.

// When G28 is called, this option will make Y home before X
#define HOME_Y_BEFORE_X

// @section machine

#define AXIS_RELATIVE_MODES {false, false, false, false}

// Allow duplication mode with a basic dual-nozzle extruder
//#define DUAL_NOZZLE_DUPLICATION_MODE

// By default pololu step drivers require an active high signal. However, some high power drivers require an active low signal as step.
#define INVERT_X_STEP_PIN false
#define INVERT_Y_STEP_PIN false
#define INVERT_Z_STEP_PIN false
#define INVERT_E_STEP_PIN false

// Default stepper release if idle. Set to 0 to deactivate.
// Steppers will shut down DEFAULT_STEPPER_DEACTIVE_TIME seconds after the last move when DISABLE_INACTIVE_? is true.
// Time can be set by M18 and M84.
#define DEFAULT_STEPPER_DEACTIVE_TIME 120
#define DISABLE_INACTIVE_X true
#define DISABLE_INACTIVE_Y true
#define DISABLE_INACTIVE_Z true  // set to false if the nozzle will fall down on your printed part when print has finished.
#define DISABLE_INACTIVE_E true

#define DEFAULT_MINIMUMFEEDRATE       0.0     // minimum feedrate
#define DEFAULT_MINTRAVELFEEDRATE     0.0

#define DEFAULT_MINSEGMENTTIME        20000

// If defined the movements slow down when the look ahead buffer is only half full
#define SLOWDOWN

#define MINIMUM_PLANNER_SPEED 0.05// (mm/sec)

// Microstep setting (Only functional when stepper driver microstep pins are connected to MCU.
#define MICROSTEP_MODES {16,16,16,16,16} // [1,2,4,8,16]

#define USE_WATCHDOG

#define AUTO_REPORT_TEMPERATURES

/**
 * Include capabilities in M115 output
 */
#define EXTENDED_CAPABILITIES_REPORT

#define PROPORTIONAL_FONT_RATIO 1.0

/**
 * Spend 28 bytes of SRAM to optimize the GCode parser
 */
#define FASTER_GCODE_PARSER

#define EMERGENCY_PARSER

#endif