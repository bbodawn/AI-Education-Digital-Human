"""WebRTC 信令代理。

浏览器只与本地 FastAPI 协商 SDP。协商成功后，音视频媒体流由 WebRTC
在浏览器与 LiveTalking 之间直接传输，不经过本服务——FastAPI 只做信令。
"""

from fastapi import APIRouter, Depends

from app.schemas import WebRTCOfferRequest, WebRTCOfferResponse
from app.services import get_service
from app.services.digital_human import DigitalHumanService
from app.session import set_session_id

router = APIRouter(prefix="/api/v1/avatar", tags=["avatar"])


@router.post("/webrtc/offer", response_model=WebRTCOfferResponse)
async def webrtc_offer(
    payload: WebRTCOfferRequest,
    service: DigitalHumanService = Depends(get_service),
) -> WebRTCOfferResponse:
    """把浏览器的 SDP offer 转交 LiveTalking，并记下它返回的 session_id。

    会话正是在这一步真正建立的；记下的 session_id 供后续 say / interrupt 使用。
    """
    session = await service.negotiate_webrtc(sdp=payload.sdp, sdp_type=payload.type)
    set_session_id(session.session_id)

    return WebRTCOfferResponse(
        sdp=session.sdp,
        type=session.type,
        session_id=session.session_id,
    )
