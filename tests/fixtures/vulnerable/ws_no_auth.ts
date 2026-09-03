// Vulnerable: Socket.IO without auth or origin check
import { Server } from "socket.io";

const io = new Server(httpServer);

io.on("connection", (socket) => {
  // No auth check on connect
  socket.on("join_room", (data) => {
    socket.join(data.room);
  });

  socket.on("send_message", (data) => {
    io.to(data.room).emit("message", data);
  });
});
