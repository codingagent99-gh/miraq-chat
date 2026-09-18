"""
channels/ - render a /chat turn for messaging channels (WhatsApp, Instagram).

The chat pipeline is untouched: routes/channel.py runs the normal /chat turn and 
hands the widget-shaped JSON to a renderer here, which turns it into the channel's native Send-API message objects.
The calling service only has to POST each object, in order, to Meta (or its BSP).

    common.py   channel-neutral parsing: markdown, reply-id codec, options
    whatsapp.py WhatsApp Cloud-API message objects
    instagram.py Instagram Messaging API message objects
"""

from channels.common import SUPPORTED_CHANNELS, decode_reply_id
from channels.whatsapp import render_whatsapp
from channels.instagram import render_instagram

RENDERERS = {
    "whatsapp": render_whatsapp,
    "instagram": render_instagram,
}

__all__ = ["SUPPORTED_CHANNELS", "RENDERERS", "decode_reply_id"]
