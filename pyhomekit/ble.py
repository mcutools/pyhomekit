"""Contains all of the HAP-BLE classes."""

import logging
import random

from struct import pack, unpack
from typing import (Any, Callable, Dict, List, Sequence)  # NOQA pylint: disable=W0611
from typing import (Tuple, Union, Optional, Iterator)  # NOQA pylint: disable=W0611

import bluepy.btle
import tenacity

from . import constants
from .utils import prepare_tlv, iterate_tvl, HapBleError, parse_ktlvs

logger = logging.getLogger(__name__)


class HapBlePduHeader:
    """Interface for HAP-BLE Headers.

    This class is not meant to be instantiated. Use the children
    HapBlePduRequestHeader and HapBlePduResponseHeader.

    Parameters
    ----------
    continuation
        indicates the fragmentation status of the HAP-BLE PDU. False
        indicates a first fragment or no fragmentation.

    response
        indicates whether the PDU is a response (versus a request)
    """

    def __init__(self, response: bool, continuation: bool) -> None:
        self.continuation = continuation
        self.response = response

    @property
    def control_field(self) -> int:
        """Get Control Field as int."""
        return int(self.control_field_bits, 2)

    @property
    def control_field_bits(self) -> str:
        """Get Control Field as string of bits."""
        control_field_str = "{continuation}00000{response}0".format(
            continuation=int(self.continuation), response=int(self.response))
        return control_field_str

    @property
    def data(self) -> bytes:
        raise NotImplementedError

    def __str__(self) -> str:
        return "continuation: {}, response: {}".format(self.continuation,
                                                       self.response)


class HapBlePduRequestHeader(HapBlePduHeader):
    """HAP-BLE PDU Request Header.

    Parameters
    ----------
    continuation
        indicates the fragmentation status of the HAP-BLE PDU. False
        indicates a first fragment or no fragmentation.

    response
        indicates whether the PDU is a response (versus a request)

    transation_id
        Transaction Identifier

    op_code
        HAP Opcode field, which indicates the opcode for the HAP Request PDU.

    cid_sid
        Characteristic / Service Instance Identifier is the instance id
        of the characteristic / service for a particular request.
    """

    def __init__(self,
                 cid_sid: bytes,
                 op_code: int,
                 response: bool=False,
                 continuation: bool=False,
                 transaction_id: int=None) -> None:
        super(HapBlePduRequestHeader, self).__init__(
            response=response, continuation=continuation)
        self.op_code = op_code
        self._transaction_id = transaction_id
        self.cid_sid = cid_sid

    @property
    def transaction_id(self) -> int:
        """Get the transaction identifier, or generate a new one if none exists.

        The transation ID is an 8 bit number identifying the transaction
        number of this PDU. The TID is randomly generated by the originator
        of the request and is used to match a request/response pair.
        """
        if self._transaction_id is None:
            self._transaction_id = random.SystemRandom().getrandbits(8)
        return self._transaction_id

    @property
    def data(self) -> bytes:
        """Byte representation of the PDU Header.

        Depends on whether it is a continuation header or not."""
        if self.continuation:
            return pack('<BB', self.control_field, self.transaction_id)
        return pack('<BBB', self.control_field, self.op_code,
                    self.transaction_id) + self.cid_sid

    def __str__(self) -> str:
        return super(HapBlePduRequestHeader, self).__str__(
        ) + " op_code: {}, transaction_id: {}, cid_sid: {}".format(
            self.op_code, self.transaction_id, self.cid_sid)


class HapBlePduResponseHeader(HapBlePduHeader):
    """HAP-BLE PDU Response Header.

    Parameters
    ----------
    continuation
        indicates the fragmentation status of the HAP-BLE PDU. False
        indicates a first fragment or no fragmentation.

    response
        indicates whether the PDU is a response (versus a request)

    transaction_id
        Transaction Identifier

    status_code
        HAP Status code for the request.
    """

    def __init__(self,
                 status_code: int,
                 transaction_id: int,
                 continuation: bool=False,
                 response: bool=True) -> None:
        super(HapBlePduResponseHeader, self).__init__(
            response=response, continuation=continuation)
        self.transaction_id = transaction_id
        self.status_code = status_code

    @classmethod
    def from_data(cls, data: bytes) -> 'HapBlePduResponseHeader':
        """Creates a header from response bytes"""

        control_field, tid, status_code = unpack('<BBB', data[:3])

        # turn control field into its bits
        control_field = bin(control_field)[2:].zfill(8)[::-1]
        continuation = control_field[7] == '1'
        response = control_field[1] == '1'
        if control_field[0] + control_field[2:7] != '000000':
            raise ValueError("Invalid control field for response header {}".
                             format(control_field))
        return HapBlePduResponseHeader(
            continuation=continuation,
            response=response,
            transaction_id=tid,
            status_code=status_code)

    @property
    def data(self) -> bytes:
        """Byte representation of the PDU Header."""
        return pack('<BBB', self.control_field, self.transaction_id,
                    self.status_code)

    def __str__(self) -> str:
        return super(
            HapBlePduResponseHeader,
            self).__str__() + " status_code: {}, transaction_id: {}".format(
                constants.HapBleStatusCodes()(self.status_code),
                self.transaction_id)


class HapBlePdu:
    """HAP BLE PDU"""
    max_len = 512

    def __init__(self,
                 header: HapBlePduHeader,
                 TLVs: List[Tuple[Union[str, int], bytes]]) -> None:
        self.header = header
        self.TLVs = TLVs

    @property
    def raw_data(self) -> bytes:
        prepared_tlvs = [
            data
            for param_type, value in self.TLVs
            for data in prepare_tlv(param_type, value)
        ]

        return self.header.data + b''.join(prepared_tlvs)

    @property
    def fragmented(self) -> bool:
        return len(self.raw_data) > self.max_len

    def pdu_fragments(self) -> Iterator[bytes]:
        yield self.raw_data


class HapCharacteristic:
    """Represents data or an associated behavior of a service.

    The characteristic is defined by a universally unique type, and has additional
    properties that determine how the value of the characteristic can be accessed.

    Parameters
    ----------
    accessory
        The accessory this characteristic belongs to.

    uuid
        The UUID of the underlying GATT characteristic

    retry
        Attempt to reconnect when error.

    retry_max_attempts
        How many times to attempt reconnection.

    retry_wait_time
        How long to wait in s between reconnection attempts.
    """

    def __init__(self,
                 accessory: 'HapAccessory',
                 uuid: str,
                 retry: bool=False,
                 retry_max_attempts: int=1,
                 retry_wait_time: int=2) -> None:
        self.uuid = uuid
        self.accessory = accessory
        self.retry = retry
        self.retry_max_attempts = retry_max_attempts
        self.retry_wait_time = retry_wait_time

        self._cid = None  # type: Optional[bytes]
        self.hap_format_converter = constants.identity
        self._signature = None  # type: Optional[Dict[str, Any]]

        if self.retry:
            self._setup_tenacity(
                max_attempts=self.retry_max_attempts,
                wait_time=self.retry_wait_time)

    def _request(self,
                 header: HapBlePduRequestHeader,
                 body: List[Tuple[int, bytes]]=None) -> None:
        """Perform a HAP read or write request."""
        logger.debug("HAP read/write request.")

        if not body:
            logger.debug("Writing header to characteristic: %s", header.data)
            self._characteristic.write(header.data, withResponse=True)
        else:
            for data in fragment_tlvs(header, body):
                logger.debug("Writing header + data to characteristic: %s",
                             data)
                self._characteristic.write(data, withResponse=True)

    def _read(self) -> bytes:
        """Read the value of the characteristic."""
        logger.debug("Reading characteristic value.")
        return self._characteristic.read()

    def write(self,
              request_header: HapBlePduRequestHeader,
              TLVs: List[Tuple[int, bytes]]) -> Dict[str, Any]:
        """Perform a HAP Characteristic write.

        Fragmented read/write if required."""
        logger.debug("HAP read/write with OpCode: %s.",
                     constants.HapBleOpCodes()(request_header.op_code))

        self._request(request_header, TLVs)

        response = self._read()
        logger.debug("Response data: %s", response)

        response_header = self._check_read_response(
            request_header=request_header, response=response)
        logger.debug("Response header: %s", response_header)
        response_parsed = self._parse_response(response)

        if response_header.continuation:
            # TODO: fragmented read
            raise NotImplementedError("Fragmented read not yet supported")

        return response_parsed

    def write_ktlvs(self,
                    request_header: HapBlePduRequestHeader,
                    kTLVs: Sequence[Tuple[int, bytes]]) -> Dict[str, Any]:
        """Perform a HAP Characteristic write for a pairing.

        Fragmented read/write if required."""
        logger.debug("HAP write pairing with OpCode: %s.",
                     constants.HapBleOpCodes()(request_header.op_code))

        assembled = {}  # type: Dict[str, Any]

        while True:
            logger.debug("Preparing message with kTLVs: %s", kTLVs)
            prepared_ktlvs = b''.join(
                data for ktlv in kTLVs for data in prepare_tlv(*ktlv))

            TLVs = [(constants.HapParamTypes.Return_Response, pack('<B', 1)),
                    (constants.HapParamTypes.Value, prepared_ktlvs)]

            response_parsed = self.write(request_header, TLVs)
            if 'value' not in response_parsed:
                raise HapBleError(
                    name="Pairing Error", message="No ktlvs received")

            parsed_ktlvs = parse_ktlvs(response_parsed['value'])

            # Check fragmentation
            if 'kTLVType_FragmentData' in parsed_ktlvs:
                logger.debug("Found kTLV FragmentData - appending")
                assembled['kTLVType_FragmentData'] = assembled.get(
                    'kTLVType_FragmentData',
                    b'') + parsed_ktlvs['kTLVType_FragmentData']
                # send new ktlv fragmentdata empty
                kTLVs = [(constants.PairingKTlvValues.kTLVType_FragmentData,
                          b'')]
            elif 'kTLVType_FragmentLast' in parsed_ktlvs:
                logger.debug(
                    "Found kTLV FragmentLast - appending final fragment")
                assembled['kTLVType_FragmentData'] = (
                    assembled['kTLVType_FragmentData'] +
                    parsed_ktlvs['kTLVType_FragmentLast'])
                break
            else:
                logger.debug("Unfragmented kTLVS - returning parsed data.")
                assembled = parsed_ktlvs
                break
        return assembled

    def read(self, request_header: HapBlePduRequestHeader) -> Dict[str, Any]:
        """Perform a HAP Characteristic read.

        Fragmented read if required."""

        response_parsed = self.write(request_header, [])

        return response_parsed

    def _setup_tenacity(self, max_attempts: int, wait_time: int) -> None:
        """Adds automatic retrying to functions that need to read from device."""
        reconnect_callback = reconnect_callback_factory(
            accessory=self.accessory)

        retry = reconnect_tenacity_retry(reconnect_callback, max_attempts,
                                         wait_time)

        retry_functions = [
            self._read_cid, self._request, self._read, self._characteristic
        ]

        for func in retry_functions:
            name = func.__name__
            setattr(self, name, retry(func))

    @property
    def _characteristic(self) -> bluepy.btle.Characteristic:
        """Returns the underlying GATT characteristic."""
        return self.accessory.charateristic(self.uuid)

    @property
    def cid(self) -> bytes:
        """Get the Characteristic ID, reading it from the device if required."""
        if self._cid is None:
            self._cid = self._read_cid()
        return self._cid

    @property
    def signature(self) -> Dict[str, Any]:
        """Returns the signature, and adds the attributes."""
        if self._signature is None:
            signature_read_header = HapBlePduRequestHeader(
                cid_sid=self.cid,
                op_code=constants.HapBleOpCodes.Characteristic_Signature_Read,
            )
            self._signature = self.read(signature_read_header)
        return self._signature

    def _read_cid(self) -> bytes:
        """Read the Characteristic ID descriptor."""
        logger.debug("Read characteristic ID descriptor.")
        cid_descriptor = self._characteristic.getDescriptors(
            constants.characteristic_ID_descriptor_UUID)[0]
        return cid_descriptor.read()

    @staticmethod
    def _check_read_response(request_header: HapBlePduRequestHeader,
                             response: bytes) -> HapBlePduResponseHeader:
        """Parses response signature and verifies validity."""

        response_header = HapBlePduResponseHeader.from_data(response)

        if not response_header.response:
            raise ValueError("Invalid control field {}, not a response.".
                             format(response_header.control_field))
        if response_header.transaction_id != request_header.transaction_id:
            raise ValueError("Invalid transaction ID {}, expected {}.".format(
                response_header.transaction_id, request_header.transaction_id),
                             response)
        if response_header.status_code != constants.HapBleStatusCodes.Success:
            raise HapBleError(status_code=response_header.status_code)

        if len(response) > 3:
            body_length = unpack('<H', response[3:5])[0]
            if len(response[5:]) != body_length:
                raise ValueError("Invalid body length {}, expected {}.".format(
                    len(response[5:]), body_length), response)

        return response_header

    def _parse_response(self, response: bytes) -> Dict[str, Any]:
        """Parse read response and set attributes."""

        logger.debug("Parse read response.")
        attributes = {}  # type: Dict[str, Any]
        for body_type, length, bytes_ in iterate_tvl(response[5:]):
            if len(bytes_) != length:
                raise HapBleError(name="Invalid response length")
            name = constants.HAP_param_type_code_to_name[body_type]

            if name in ('GATT_Valid_Range', 'HAP_Step_Value_Descriptor',
                        'Value'):
                converter = self.hap_format_converter
            else:
                converter = constants.HAP_param_name_to_converter[name]

            # Treat GATT_Presentation_Format_Descriptor specially
            if name == 'GATT_Presentation_Format_Descriptor':
                format_code, unit_code = converter(bytes_)
                format_name = constants.format_code_to_name[format_code]
                format_converter = constants.format_name_to_converter[
                    format_name]
                unit_name = constants.unit_code_to_name[unit_code]
                new_attrs = {
                    'HAP_Format': format_name,
                    'HAP_Format_Converter': format_converter,
                    'HAP_Unit': unit_name
                }

            # List of values received in the HAP Format
            elif name == 'GATT_Valid_Range':
                low, high = bytes_[:len(bytes_) // 2], bytes_[
                    len(bytes_) // 2:]
                new_attrs = {
                    'min_value': converter(low),
                    'max_value': converter(high)
                }
            else:
                new_attrs = {name: converter(bytes_)}

            # Add new attributes
            for key, val in new_attrs.items():
                logger.debug("TLV found in response. %s: %s", key, val)
                key = key.lower()
                if key in attributes:
                    logger.debug(
                        "Duplicate TLV Param Type found: %s. Appending.", key)
                    val = attributes[key] + val
                setattr(self, key, val)
                attributes[key] = val

        return attributes


class HapAccessory:
    """HAP Accesory.

    Parameters
    ----------
    address
        MAC address of the accessory

    address_type
        Type of the address: static or random
    """

    def __init__(self, address: str, address_type: str='static') -> None:
        self.address = address
        self.address_type = address_type
        self.peripheral = bluepy.btle.Peripheral()
        self._characteristics = {
        }  # type: Dict[str, bluepy.btle.Characteristic]

    def connect(self) -> None:
        """Connect to BLE peripheral."""
        self.peripheral.connect(self.address, self.address_type)

    def charateristic(self, uuid: str) -> bluepy.btle.Characteristic:
        """Return the GATT characteristic for the given UUID."""
        if uuid not in self._characteristics:
            characteristic = self.peripheral.getCharacteristics(uuid=uuid)[0]
            self._characteristics[uuid] = characteristic
        return self._characteristics[uuid]

    def pair(self) -> None:
        pass

    def pair_verify(self) -> None:
        pass

    def save_key(self) -> None:
        pass

    def discover_hap_characteristics(self) -> List[HapCharacteristic]:
        """Discovers all of the HAP Characteristics and performs a signature read on each one."""
        pass

    def get_characteristic(self, name: str, uuid: str) -> HapCharacteristic:
        pass


class HapAccessoryLock(HapAccessory):

    # Required
    def lock_current_state(self) -> int:
        pass

    # Required
    def lock_target_state(self) -> None:
        pass

    # Required for lock management
    def lock_control_point(self) -> Any:
        pass

    def version(self) -> str:
        pass

    # Optional for lock management
    def logs(self) -> str:
        pass

    def audio_feedback(self) -> bytes:
        pass

    def lock_management_auto_security_timeout(self) -> None:
        pass

    def administrator_only_access(self) -> None:
        pass

    def lock_last_known_action(self) -> int:
        pass

    def current_door_state(self) -> int:
        pass

    def motion_detected(self) -> bool:
        pass


def reconnect_callback_factory(
        accessory: HapAccessory) -> Callable[[Any, int], None]:
    """Factory for creating tenacity before callbacks to reconnect to a peripheral."""

    # pylint: disable=W0613
    def reconnect(func: Any, trial_number: int) -> None:
        """Attempt to reconnect."""
        try:
            logger.debug("Attempting to reconnect to device.")
            accessory.connect()
        except bluepy.btle.BTLEException:
            logger.debug(
                "Error while attempting to reconnect to device", exc_info=True)

    return reconnect


def reconnect_tenacity_retry(reconnect_callback: Callable[[Any, int], Any],
                             max_attempts: int=2,
                             wait_time: int=2) -> tenacity.Retrying:
    """Build tenacity retry object"""
    retry = tenacity.retry(
        stop=tenacity.stop_after_attempt(max_attempts),
        wait=tenacity.wait_fixed(wait_time),
        retry=tenacity.retry_if_exception_type(bluepy.btle.BTLEException),
        before=reconnect_callback)

    return retry


def fragment_tlvs(header: HapBlePduRequestHeader,
                  TLVs: List[Tuple[int, bytes]]) -> Iterator[bytes]:
    """Returns the fragmented TLVs to write."""
    logger.debug("Preparing data for characteristic write: %s", TLVs)

    prepared_tlvs = [
        data
        for param_type, value in TLVs
        for data in prepare_tlv(param_type, value)
    ]

    body_concat = b''.join(prepared_tlvs)

    max_len = 512

    # Is a fragmented write necessary?
    if len(header.data) + 2 + len(body_concat) <= max_len:
        data = header.data + pack('<H', len(body_concat)) + body_concat
        logger.debug("No fragmentation necessary.")
        yield data
    else:
        logger.debug("Fragmentation necessary. Total len %s", len(body_concat))
        while prepared_tlvs:
            # Fill fragment
            fragment_data = b''
            while prepared_tlvs and len(fragment_data) + len(
                    prepared_tlvs[0]) < max_len:
                logger.debug("Add to fragment: %s", prepared_tlvs[0])
                fragment_data += prepared_tlvs.pop(0)

            data = header.data + pack('<H', len(fragment_data)) + fragment_data

            yield data

            # Future fragments are continuations
            header.continuation = True
