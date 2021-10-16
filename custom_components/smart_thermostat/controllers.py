import abc
import logging
from typing import Optional, final, Mapping, Any

from simple_pid import PID

from homeassistant.components.climate import HVAC_MODE_OFF, HVAC_MODE_COOL, HVAC_MODE_HEAT, HVAC_MODE_HEAT_COOL
from homeassistant.const import STATE_ON, ATTR_ENTITY_ID, SERVICE_TURN_ON, SERVICE_TURN_OFF
from homeassistant.core import DOMAIN as HA_DOMAIN, callback, Event, HomeAssistant, Context, CALLBACK_TYPE, State
from homeassistant.exceptions import ConditionError
from homeassistant.helpers import condition
from homeassistant.helpers.event import async_track_state_change_event

ATTR_PID_PARAMS = "pid_params"

_LOGGER = logging.getLogger(__name__)


class Thermostat(abc.ABC):
    @abc.abstractmethod
    def get_entity_id(self) -> str:
        """Get Entity name instance"""

    @abc.abstractmethod
    def get_hvac_mode(self) -> str:
        """Get Current HVAC mode"""

    @abc.abstractmethod
    def get_context(self) -> Context:
        """Get Context instance"""

    @abc.abstractmethod
    def get_target_temperature(self):
        """Return the target temperature."""

    @abc.abstractmethod
    def get_current_temperature(self):
        """Return the sensor temperature."""

    @abc.abstractmethod
    def async_write_ha_state(self) -> None:
        """Write thermostat state."""

    @abc.abstractmethod
    def async_on_remove(self, func: CALLBACK_TYPE) -> None:
        """Add callback"""


class AbstractController(abc.ABC):
    """
    Abstract controller
    """

    def __init__(
            self,
            name: str,
            mode: str,
            target_entity_id: str,
            inverted: bool
    ):
        self.__thermostat: Optional[Thermostat] = None
        self.name = name
        self._mode = mode
        self._target_entity_id = target_entity_id
        self._inverted = inverted
        self.__running = False
        self._hass = Optional[HomeAssistant]
        if mode not in [HVAC_MODE_COOL, HVAC_MODE_HEAT]:
            raise ValueError(f"Unsupported mode: '{mode}'")

    def set_thermostat(self, thermostat: Thermostat):
        self.__thermostat = thermostat

    @property
    @final
    def _hvac_mode(self) -> str:
        return self.__thermostat.get_hvac_mode()

    @property
    @final
    def _context(self) -> Context:
        return self.__thermostat.get_context()

    @property
    @final
    def _thermostat_entity_id(self) -> str:
        return self.__thermostat.get_entity_id()

    @property
    def extra_state_attributes(self) -> Optional[Mapping[str, Any]]:
        return None

    async def async_added_to_hass(self, hass: HomeAssistant, old_state: State):
        """Will be called in Entity async_added_to_hass()"""
        self._hass = hass

        self.__thermostat.async_on_remove(
            async_track_state_change_event(
                self._hass, [self._target_entity_id], self._on_target_entity_state_changed
            )
        )

    def async_startup(self):
        """
        Startup method. Will ve called after HA core started
        """
        self._hass.create_task(self.async_control())

    @callback
    def _on_target_entity_state_changed(self, event: Event):
        """On state changed callback"""
        _ = event
        self._hass.create_task(self.async_control())

        # notify to handle correct current HVAC mode
        self.__thermostat.async_write_ha_state()

    @property
    def running(self):
        return self.__running

    @abc.abstractmethod
    def is_working(self):
        """Is target working now?"""

    @final
    async def async_start(self):
        cur_temp = self.__thermostat.get_current_temperature()
        target_temp = self.__thermostat.get_target_temperature()

        if await self._async_start(cur_temp, target_temp):
            _LOGGER.debug(
                "%s: %s - Started controller, cur: %, target: %s "
                "Activated",
                self._thermostat_entity_id,
                self.name,
                cur_temp,
                target_temp,
            )
            self.__running = True
        else:
            _LOGGER.error(
                "%s: %s - Error starting controller, cur: %, target: %s "
                "Activated",
                self._thermostat_entity_id,
                self.name,
                cur_temp,
                target_temp,
            )

    @final
    async def async_stop(self):
        _LOGGER.debug(
            "%s: %s - Stopping controller",
            self._thermostat_entity_id,
            self.name
        )
        await self._async_stop()
        self.__running = False

    @abc.abstractmethod
    async def _async_start(self, cur_temp, target_temp) -> bool:
        """Start controller implementation"""

    @abc.abstractmethod
    async def _async_stop(self):
        """Stop controller implementation"""

    @final
    async def async_control(self, time=None, force=False):
        """Callback which will be called from Climate Entity"""
        if not self.__running:
            return

        cur_temp = self.__thermostat.get_current_temperature()
        target_temp = self.__thermostat.get_target_temperature()

        await self._async_control(cur_temp, target_temp, time=time, force=force)

    @abc.abstractmethod
    async def _async_control(self, cur_temp, target_temp, time=None, force=False):
        """Control method. Should be overwritten in child classes"""


class PidParams(abc.ABC):
    def __init__(self, kp, ki, kd):
        self.kp = kp
        self.ki = ki
        self.kd = kd

    def invert(self):
        self.kp = -self.kp
        self.ki = -self.ki
        self.kd = -self.kd


class AbstractPidController(AbstractController, abc.ABC):
    def __init__(
            self,
            name: str,
            mode,
            target_entity_id: str,
            pid_params: PidParams,
            inverted: bool
    ):
        super().__init__(name, mode, target_entity_id, inverted)
        self._initial_pid_params = pid_params
        self._current_pid_params = Optional[PidParams]
        self._pid = Optional[PID]

    def is_working(self):
        raise NotImplementedError()  # FIXME: Not implemented

    @final
    async def async_added_to_hass(self, hass: HomeAssistant, old_state: State):
        await super().async_added_to_hass(hass, old_state)

        if old_state is not None and old_state.attributes.get(ATTR_PID_PARAMS) is not None:
            saved_pid_params = old_state.attributes.get(ATTR_PID_PARAMS)
            if saved_pid_params:
                kp, ki, kd = saved_pid_params.split(',')
                self._current_pid_params = PidParams(kp, ki, kd)
                _LOGGER.info("%s: %s - restored last PID params: %s",
                             self._thermostat_entity_id,
                             self.name,
                             self._current_pid_params
                             )
        if not self._current_pid_params:
            self._current_pid_params = self._initial_pid_params
            _LOGGER.info("%s: %s - No PID params found in state attributes, using default: %s",
                         self._thermostat_entity_id,
                         self.name,
                         self._current_pid_params if self._current_pid_params else None
                         )

    @property
    def extra_state_attributes(self) -> Optional[Mapping[str, Any]]:
        p = self._current_pid_params
        return {
            ATTR_PID_PARAMS: f"{p.kp},{p.ki},{p.kd}" if p else None
        }

    @final
    def set_pid_params(self, pid_params: PidParams):
        """Set new PID params."""
        if not pid_params:
            raise ValueError(f"PID params can't be None")

        if self._mode == HVAC_MODE_COOL and not pid_params.kp < 0:
            pid_params.invert()
            _LOGGER.warning("%s: %s - Cooler mode but kp not negative. Inverting all PID params: %s",
                            self._thermostat_entity_id,
                            self.name,
                            pid_params
                            )
        if self._inverted:
            pid_params.invert()
            _LOGGER.info("%s: %s - Target behavior inverted requested in config. Inverting all PID params: %s",
                         self._thermostat_entity_id,
                         self.name,
                         pid_params
                         )

        self._current_pid_params = pid_params

        if self._pid:
            self._pid.Kp = pid_params.kp
            self._pid.Ki = pid_params.ki
            self._pid.Kd = pid_params.kd

        _LOGGER.info("%s: %s - New PID params: %s",
                     self._thermostat_entity_id,
                     self.name,
                     self._current_pid_params
                     )

    @final
    async def _async_start(self, cur_temp, target_temp) -> bool:

        if not self._current_pid_params:
            _LOGGER.error("%s: %s - Start called but no PID params was set", self._thermostat_entity_id, self.name)
            return False

        pid_params = self._current_pid_params
        self._pid = PID(
            pid_params.kp, pid_params.ki, pid_params.kp,
            setpoint=target_temp,
            output_limits=self._get_output_limits()
        )

        current_output = self._hass.states.get(self._target_entity_id)
        if current_output:
            self._pid.set_auto_mode(enabled=True, last_output=current_output)

        _LOGGER.info("%s: %s - Initialized.  PID params: %s, current output: %s",
                     self._thermostat_entity_id,
                     self.name,
                     pid_params,
                     current_output
                     )
        return True

    @final
    async def _async_stop(self):
        self._pid = None
        pass

    @final
    async def _async_control(self, cur_temp, target_temp, time=None, force=False):
        if self._pid.setpoint != target_temp:
            _LOGGER.info("%s: %s - Target setpoint was changed from %s to %s",
                         self._thermostat_entity_id,
                         self.name,
                         self._pid.setpoint,
                         target_temp
                         )
            self._pid.setpoint = target_temp

        output = float(self._pid(cur_temp))

        current_output = self._hass.states.get(self._target_entity_id)
        if current_output != output:
            _LOGGER.debug("%s: %s - Current temp: %s, target temp: %s, adjusting from %s to %s",
                          self._thermostat_entity_id,
                          self.name,
                          cur_temp,
                          target_temp,
                          output,
                          current_output
                          )
            self._apply_output(output)

    @abc.abstractmethod
    def _get_output_limits(self):
        """Get output limits (min,max)"""

    @abc.abstractmethod
    def _apply_output(self, output: float):
        """Apply output to target"""


class SwitchController(AbstractController):

    def __init__(
            self,
            name: str,
            mode,
            target_entity_id: str,
            cold_tolerance: float,
            hot_tolerance: float,
            inverted: bool,
            min_cycle_duration
    ):
        super().__init__(name, mode, target_entity_id, inverted)
        self._cold_tolerance = cold_tolerance
        self._hot_tolerance = hot_tolerance
        self._min_cycle_duration = min_cycle_duration

    def is_working(self):
        return self._is_on()

    async def _async_turn_on(self):
        """Turn toggleable device on."""
        service = SERVICE_TURN_ON if not self._inverted else SERVICE_TURN_OFF
        data = {ATTR_ENTITY_ID: self._target_entity_id}
        await self._hass.services.async_call(
            HA_DOMAIN, service, data, context=self._context
        )

    async def _async_turn_off(self):
        """Turn toggleable device off."""
        service = SERVICE_TURN_OFF if not self._inverted else SERVICE_TURN_ON
        data = {ATTR_ENTITY_ID: self._target_entity_id}
        await self._hass.services.async_call(
            HA_DOMAIN, service, data, context=self._context
        )

    def _is_on(self):
        return self._hass.states.is_state(self._target_entity_id, STATE_ON)

    async def _async_start(self, cur_temp, target_temp) -> bool:
        return True

    async def _async_stop(self):
        await self._async_turn_off()

    async def _async_control(self, cur_temp, target_temp, time=None, force=False):
        # If the `force` argument is True, we
        # ignore `min_cycle_duration`.
        # If the `time` argument is not none, we were invoked for
        # keep-alive purposes, and `min_cycle_duration` is irrelevant.
        if not force and time is None and self._min_cycle_duration:
            if self._is_on():
                current_state = STATE_ON
            else:
                current_state = HVAC_MODE_OFF
            try:
                long_enough = condition.state(
                    self._hass,
                    self._target_entity_id,
                    current_state,
                    self._min_cycle_duration,
                )
            except ConditionError:
                long_enough = False

            if not long_enough:
                return

        too_cold = cur_temp <= target_temp - self._cold_tolerance
        too_hot = cur_temp >= target_temp + self._hot_tolerance

        need_turn_on = False
        if (
                too_hot and
                self._mode == HVAC_MODE_COOL and
                self._hvac_mode in [HVAC_MODE_COOL, HVAC_MODE_HEAT_COOL]
        ) or (
                too_cold and
                self._mode == HVAC_MODE_HEAT and
                self._hvac_mode in [HVAC_MODE_HEAT, HVAC_MODE_HEAT_COOL]
        ):
            need_turn_on = True

        _LOGGER.debug(f"%s: %s - too_hot: %s, too_cold: %s, need_turn_on: %s, is on: %s, (cur: %s, target: %s)",
                      self._thermostat_entity_id,
                      self.name,
                      too_hot,
                      too_cold,
                      need_turn_on,
                      self._is_on(),
                      cur_temp,
                      target_temp
                      )

        if self._is_on():
            if not need_turn_on:
                _LOGGER.info("%s: Turning off %s %s",
                             self._thermostat_entity_id,
                             self.name, self._target_entity_id)
                await self._async_turn_off()
            elif time is not None:
                # The time argument is passed only in keep-alive case
                _LOGGER.info("%s: Keep-alive - Turning on %s %s",
                             self._thermostat_entity_id,
                             self.name, self._target_entity_id)
                await self._async_turn_on()
        else:
            if need_turn_on:
                _LOGGER.info("%s: Turning on %s %s",
                             self._thermostat_entity_id,
                             self.name, self._target_entity_id)
                await self._async_turn_on()
            elif time is not None:
                # The time argument is passed only in keep-alive case
                _LOGGER.info(
                    "%s: Keep-alive - Turning off %s %s",
                    self._thermostat_entity_id,
                    self.name, self._target_entity_id
                )
                await self._async_turn_off()


class ClimatePidController(AbstractPidController):
    def __init__(
            self,
            name: str,
            mode,
            target_entity_id: str,
            pid_params: PidParams,
            inverted: bool
    ):
        super().__init__(name, mode, target_entity_id, pid_params, inverted)
